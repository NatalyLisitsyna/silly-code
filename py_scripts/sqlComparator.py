import datetime
import configparser
import os
import sys
from .helpers import configHelper, converters, dbHelper, helper
from .helpers.loggingHelper import Logger
from multiprocessing import Pool

# TODO: fix bug with multiple writing same sql-queries in some cases (potentially problem with starmap)
# TODO: add code for comparing by some sections
# TODO: add log function to make logging great again
# TODO: add hideWarnings property

logger = Logger(20)

propertyFile = "./resources/properties/sqlComparator.properties"
config = configHelper.ifmsConfigCommon(propertyFile)
sendMailFrom = config.getPropertyFromMainSection("sendMailFrom")
sendMailTo = config.getPropertyFromMainSection("sendMailTo")
mailPassword = config.getPropertyFromMainSection("mailPassword")

# sqlProperties
attempts = config.getProperty("sqlProperties", "retryAttempts")  # amount of attempts of retrying SQL-query
comparingStep = config.getProperty("sqlProperties", "comparingStep")
depthReportCheck = config.getProperty("sqlProperties", "depthReportCheck")
enableSchemaChecking = config.getProperty("sqlProperties", "enableSchemaChecking")
excludedTables = config.getProperty("sqlProperties", "tablesNotToCompare")
failWithFirstError = config.getProperty("sqlProperties", "failWithFirstError")
hideSQLQueries = config.getProperty("sqlProperties", "hideSQLQueries")
mode = config.getProperty("sqlProperties", "reportCheckType")
schemaColumns = config.getProperty("sqlProperties", "includeSchemaColumns")
hideColumns = config.getProperty("sqlProperties", "hideColumns")

def calculateDate(days):
    return (datetime.datetime.today().date() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def checkDateList(table, emptyTables, emptyProdTables, emptyTestTables, client):
    comparingTimeframe = []
    selectQuery = "SELECT distinct(dt) from %s;" % table
    sqlParamArray = Pool(2).map(configHelper.ifmsConfigClient(propertyFile, client).getSQLConnectParams, ["prod", "test"])
    dateList = dbHelper.dbConnector.runParallelSelect(sqlParamArray, client, selectQuery)
    if all(dateList):
        actualDates = set()
        for days in range(1, depthReportCheck):
            actualDates.add(calculateDate(days))
        if dateList[0][-depthReportCheck:] == dateList[1][-depthReportCheck:]:
            for item in dateList[0][-depthReportCheck:]:
                comparingTimeframe.append(item.get("dt").date().strftime("%Y-%m-%d"))
            return comparingTimeframe
        else:
            pool = Pool(2)
            dateSet = pool.map(converters.convertToSet, dateList)
            if (dateSet[0] - dateSet[1]):
                uniqueDates = getUniqueReportDates(dateSet[0], dateSet[1])
                # TODO: replace "test-db" on db-name
                # print(str(datetime.datetime.now()) + " [WARN] This dates absent in test-db: " + ",".join(uniqueDates) + " in report table " + table)
                logger.warn("This dates absent in test-db: {} in report table {}...".format(",".join(uniqueDates), table))
            if (dateSet[1] - dateSet[0]):
                uniqueDates = getUniqueReportDates(dateSet[1], dateSet[0])
                # TODO: replace "prod-db" on db-name
                # print(str(datetime.datetime.now()) + " [WARN] This dates absent in prod-db: " + ",".join(uniqueDates) + " in report table " + table)
                logger.warn("This dates absent in prod-db: {} in report table {}...".format(",".join(uniqueDates), table))
            return dateSet[0] & dateSet[1]
    else:
        if not dateList[0] and not dateList[1]:
            # print(str(datetime.datetime.now()) + " [WARN] Table " + table + " is empty in both dbs...")
            logger.warn("Table {} is empty in both dbs...".format(table))
            emptyTables.append(table)
        elif not dateList[0]:
            # print(str(datetime.datetime.now()) + " [WARN] Table " + table + " on prod-db is empty!")
            # TODO: replace "prod-db" on db-name
            logger.warn("Table {} on prod-db is empty!".format(table))
            emptyProdTables.append(table)
        else:
            # TODO: replace "test-db" on db-name
            # print(str(datetime.datetime.now()) + " [WARN] Table " + table + " on test-db is empty!")
            logger.warn("Table {} on test-db is empty!".format(table))
            emptyTestTables.append(table)
        return []


def compareData(tables, tablesWithDifferentSchema, globalBreak, noCrossedDatesTables, emptyTables, emptyProdTables, emptyTestTables, differingTables):
    tables = prepareTableList(tables, tablesWithDifferentSchema)
    mapping, columnsWithoutAssociateTable = prepareColumnMapping("prod", client)
    for table in tables:
        stopCheckingThisTable = False
        if ("report" or "statistic") in table:
            if not globalBreak:
                dates = converters.convertToList(checkDateList(table, emptyTables, emptyProdTables, emptyTestTables, client))
                dates.sort()
                if dates:
                    amountRecords = countTableRecords(table, dates[0])
                    for dt in reversed(dates):
                        if not all([globalBreak, stopCheckingThisTable]):
                            maxAmountOfRecords = max(amountRecords[0][0].get("COUNT(*)"), amountRecords[1][0].get("COUNT(*)"))
                            queryList = queryReportConstruct(table, dt, mode, maxAmountOfRecords, comparingStep, mapping)
                            globalBreak, stopCheckingThisTable = iterationComparingByQueries(queryList, globalBreak, table)
                        else:
                            break
                else:
                    # print(str(datetime.datetime.now()) + " [WARN] Tables " + table + " shouldn"t be compared correctly, because they have no any crosses dates in reports")
                    logger.warn("Tables {} should not be compared correctly, because they have no any crosses dates in reports".format(table))
                    noCrossedDatesTables.append(table)
            else:
                break
        else:
            amountRecords = countTableRecords(table, None)
            maxAmountOfRecords = max(amountRecords[0][0].get("COUNT(*)"), amountRecords[1][0].get("COUNT(*)"))
            queryList = queryEntityConstructor(table, maxAmountOfRecords, comparingStep, mapping)
            if not globalBreak:
                for query in queryList:
                    if (not compareEntityTable(table, query, differingTables)) and failWithFirstError:
                        # print(str(datetime.datetime.now()) + " [FINISHED] First error founded, checkin failed. Comparing takes " + str(datetime.datetime.now() - startTime))
                        logger.info("First error founded, checkin failed. Comparing takes {}".format(datetime.datetime.now() - startTime))
                        globalBreak = True
                        break
            else:
                break
    dataComparingTime = datetime.datetime.now() - startTime
    # print(str(datetime.datetime.now()) + " [INFO] Comparing finished in " + str(dataComparingTime))
    logger.info("Comparing finished in {}".format(dataComparingTime))
    return dataComparingTime


def compareEntityTable(table, query, differingTables):
    listEntities = getTableData(dbHelper.dbConnector.runParallelSelect(query, client))
    uniqFor0 = listEntities[0] - listEntities[1]
    uniqFor1 = listEntities[1] - listEntities[0]
    if len(uniqFor0) > 0:
        writeUniqueEntitiesToFile(table, uniqFor0, "prod")
    if len(uniqFor1) > 0:
        writeUniqueEntitiesToFile(table, uniqFor1, "test")
    if not all([len(uniqFor0) == 0, len(uniqFor1) == 0]):
        # print(str(datetime.datetime.now()) + " [ERROR] Tables " + table + " differs!")
        logger.error("Tables {} differs!".format(table))
        differingTables.append(table)
        return False
    else:
        return True


def compareReportSums(table, query, differingTables):
    # TODO: fix!
    listReports = dbHelper.dbConnector.runParallelSelect(query)
    clicks = imps = True
    prodClicks = int(listReports[0][0].get("SUM(CLICKS)"))
    testClicks = int(listReports[1][0].get("SUM(CLICKS)"))
    prodImps = int(listReports[0][0].get("SUM(CLICKS)"))
    testImps = int(listReports[1][0].get("SUM(CLICKS)"))
    if prodClicks != testClicks:
        clicks = False
        # print(str(datetime.datetime.now()) + " [WARN] There are different click sums for query " + query + ". Prod clicks=" + str(prodClicks) + ", test clicks=" + str(testClicks))
        logger.warn("There are different click sums for query {}. Prod clicks={}, test clicks={}".format(query, prodClicks, testClicks))
    if prodImps != testImps:
        imps = False
        # print(str(datetime.datetime.now()) + " [WARN] There are different imp sums for query " + query + ". Prod imps=" + str(prodImps) + ", test imps=" + str(testImps))
        logger.warn("There are different imp sums for query {}. Prod imps={}, test imps={}".format(query, prodImps, testImps))
    if not all([clicks, imps]):
        # print(str(datetime.datetime.now()) + " [ERROR] Tables " + table + " differs!")
        logger.error("Tables {} differs!".format(table))
        differingTables.append(table)
        return False
    else:
        return True


def compareReportDetailed(table, query):
    # TODO: fix
    txtReports = getTableData(dbHelper.dbConnector.runParallelSelect(query))
    uniqFor0 = txtReports[0] - txtReports[1]
    uniqFor1 = txtReports[1] - txtReports[0]
    if len(uniqFor0) > 0:
        writeUniqueEntitiesToFile(table, uniqFor0, "prod")
    if len(uniqFor1) > 0:
        writeUniqueEntitiesToFile(table, uniqFor1, "test")
    if not all([len(uniqFor0) == 0, len(uniqFor1) == 0]):
        # print(str(datetime.datetime.now()) + " [ERROR] Tables " + table + " differs!")
        logger.error("Tables {} differs!".format(table))
        differingTables.append(table)
        return False
    else:
        return True


def compareTableLists():
    selectQuery = "SELECT DISTINCT(TABLE_NAME) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'DBNAME';"
    # TODO: fix!
    tableDicts = dbHelper.dbConnector.runParallelSelect(selectQuery)
    if tableDicts[0] == tableDicts[1]:
        return tableDicts[0]
    else:
        pool = Pool(2)
        tableSets = pool.map(converters.convertToSet, tableDicts)
        prodUniqueTables = tableSets[0] - tableSets[1]
        testUniqueTables = tableSets[1] - tableSets[0]
        if len(prodUniqueTables) > 0:
            # print(str(datetime.datetime.now()) + " [WARN] Tables, which unique for production db " + str(prodUniqueTables))
            logger.warn("Tables, which unique for production db {}.".format(prodUniqueTables))
        if len(testUniqueTables) > 0:
            # print(str(datetime.datetime.now()) + " [WARN] Tables, which unique for test db " + str(testUniqueTables))
            logger.warn("Tables, which unique for test db {}.".format(testUniqueTables))
        if len(tableSets[0]) >= len(tableSets[1]):
            for item in prodUniqueTables:
                tableSets[0].remove(item)
            return converters.convertToList(tableSets[0])
        else:
            for item in testUniqueTables:
                tableSets[1].remove(item)
            return converters.convertToList(tableSets[1])


def compareTablesMetadata(tables):
    tablesWithDifferentSchema = []
    for table in tables:
        # print(str(datetime.datetime.now()) + " Check schema for table " + table + "...")
        logger.info("Check schema for table {}...".format(table))
        selectQuery = "SELECT %s FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'DBNAME' and TABLE_NAME='TABLENAME' ORDER BY COLUMN_NAME;".replace("TABLENAME", table) % schemaColumns
        columnList = getTableData(dbHelper.dbConnector.runParallelSelect(selectQuery))
        uniqForProd = columnList[0] - columnList[1]
        uniqForTest = columnList[1] - columnList[0]
        if len(uniqForProd) > 0:
            # print(str(datetime.datetime.now()) +" [ERROR] Elements, unique for table " + table + " on prod-server:" + str(uniqForProd))
            logger.error("Elements, unique for table {} on prod-server:{}".format(table, uniqForProd))
        if len(uniqForTest) > 0:
            # print(str(datetime.datetime.now()) +" [ERROR] Elements, unique for table " + table + " on test-server:" + str(uniqForTest))
            logger.error("Elements, unique for table {} on test-server:{}".format(table, uniqForTest))
        if not all([len(uniqForProd) == 0, len(uniqForTest) == 0]):
            # print(str(datetime.datetime.now()) + " [ERROR] Tables " + table + " differs!")
            logger.error(" [ERROR] Tables {} differs!".format(table))
            tablesWithDifferentSchema.append(table)
            if failWithFirstError:
                # print(str(datetime.datetime.now()) + "[FINISHED] First error founded, checking failed...")
                logger.critical("First error founded, checking failed...")
                return False
    return tablesWithDifferentSchema


def countTableRecords(table, date):
    if date is None:
        query = "SELECT COUNT(*) FROM %s;" % table
    else:
        query = "SELECT COUNT(*) FROM %s WHERE dt > '%s';" % (table, date)
    # TODO: fix!
    amountRecords = dbHelper.dbConnector.runParallelSelect(query)
    return amountRecords


def createTestDir(path, client):
    if not os.path.exists(path):
        os.mkdir(path)
    if not os.path.exists(path + client):
        os.mkdir(path + client)


def generateMailText(emptyTables, differingTables, noCrossedDatesTables, columnsWithoutAssociateTable, prodUniqueTables, testUniqueTables):
    body = "Initial conditions:\n\n"
    if enableSchemaChecking:
        body = body + "1. Schema checking enabled.\n"
    else:
        body = body + "1. Schema checking disabled.\n"
    if failWithFirstError:
        body = body + "2. Failed with first founded error.\n"
    else:
        body = body + "2. Find all errors\n"
    body = body + "3. Report checkType is " + config["main"]["reportCheckType"] + "\n\n"
    if any([emptyTables, differingTables, noCrossedDatesTables, columnsWithoutAssociateTable, prodUniqueTables, testUniqueTables]):
        body = body + "There are some problems found during checking.\n\n"
        if emptyTables:
            body = body + "Tables, empty in both dbs:\n" + ",".join(emptyTables) + "\n\n"
        if emptyProdTables:
            body = body + "Tables, empty on production db:\n" + ",".join(emptyProdTables) + "\n\n"
        if emptyTestTables:
            body = body + "Tables, empty on test db:\n" + ",".join(emptyTestTables) + "\n\n"
        if differingTables:
            body = body + "Tables, which have any difference:\n" + ",".join(differingTables) + "\n\n"
        if list(set(emptyTables).difference(set(noCrossedDatesTables))):
            body = body + "Report tables, which have no crossing dates:\n" + ",".join(list(set(emptyTables).difference(set(noCrossedDatesTables)))) + "\n\n"
        if prodUniqueTables:
            body = body + "Tables, which unique for production db:\n" + ",".join(converters.convertToList(prodUniqueTables)) + "\n\n"
        if testUniqueTables:
            body = body + "Tables, which unique for test db:\n" + ",".join(converters.convertToList(testUniqueTables)) + "\n\n"
        if columnsWithoutAssociateTable:
            body = body + "Columns, which not associated with tables, but probably should:\n" + ",".join(columnsWithoutAssociateTable)
    else:
        body = body + "It is impossible! There is no any problems founded!"
    if enableSchemaChecking:
        body = body + "Schema checked in " + str(schemaComparingTime) + "\n"
    body = body + "Dbs checked in " + str(dataComparingTime) + "\n"
    return body


def getColumnList(stage, table, client):
    # Function returns column list for sql-query for report table
    sql = dbHelper.dbConnector(clientConfig.getSQLConnectParams(stage), client)
    try:
        with sql.connection.cursor() as cursor:
            columnList = []
            queryGetColumnList = "SELECT column_name FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name = '%s' AND table_schema = '%s';" % (table, sql.db)
            if "False" in hideSQLQueries:
                print(queryGetColumnList)
            cursor.execute(queryGetColumnList)
            columnDict = cursor.fetchall()
            for i in columnDict:
                element = "t." + str(i.get("column_name")).lower()  # It"s neccessary to make possible queries like "select key form keyname;"
                columnList.append(element)
            for column in hideColumns.split("|"):
                if "t." + column.replace("_", "") in columnList:
                    columnList.remove("t." + column)
            if columnList == []:
                return False
            columnString = ",".join(columnList)
            return columnString.lower()
    finally:
        sql.connection.close()


def getTableData(listReports):
    txtReports = []
    for record in listReports:
        instance = set()
        for item in record:
            section = []
            keys = list(item.keys())
            keys.sort()
            for key in keys:
                try:
                    section.append(str(int(item.get(key))))
                except (TypeError, ValueError):
                    section.append(str(item.get(key)))
            instance.add(",".join(section))
        txtReports.append(instance)
    return txtReports


def getUniqueReportDates(firstDateList, secondDateList):
    uniqueDates = []
    for item in converters.convertToList(firstDateList - secondDateList):
        uniqueDates.append(item.strftime("%Y-%m-%d %H:%M:%S"))
    return uniqueDates


def iterationComparingByQueries(queryList, globalBreak, table):
    stopCheckingThisTable = False
    for query in queryList:
        if mode == "day-sum":
            if ("impressions" and "clicks") in getColumnList("prod", table):
                if not compareReportSums(table, query, differingTables) and failWithFirstError:
                    # print(str(datetime.datetime.now()) + " [FINISHED] First error founded, checking failed. Comparing takes " + str(datetime.datetime.now() - startTime))
                    logger.critical("First error founded, checking failed. Comparing takes {}.".format(datetime.datetime.now() - startTime))
                    globalBreak = True
                    return globalBreak, stopCheckingThisTable
            else:
                # print(str(datetime.datetime.now()) + " [WARN] There is no impression of click column in table " + table)
                logger.warn("There is no impression of click column in table {}".format(table))
                stopCheckingThisTable = True
                return globalBreak, stopCheckingThisTable
        elif mode == "section-sum" or mode == "detailed":
            if not compareReportDetailed(table, query) and failWithFirstError:
                # print(str(datetime.datetime.now()) + " [FINISHED] First error founded, checking failed. Comparing takes " + str(datetime.datetime.now() - startTime))
                logger.critical("First error founded, checking failed. Comparing takes {}.".format(datetime.datetime.now() - startTime))
                globalBreak = True
                return globalBreak, stopCheckingThisTable
    return globalBreak, stopCheckingThisTable


def prepareColumnMapping(stage, client):
    sql = dbHelper.dbConnector(clientConfig.getSQLConnectParams(stage), client).connection
    try:
        with sql.connection.cursor() as cursor:
            columnList = []
            mapping = {}
            columnsWithoutAssociateTable = []
            queryGetColumn = "SELECT distinct(column_name) FROM INFORMATION_SCHEMA.COLUMNS WHERE table_schema = '%s' AND column_name LIKE '%id' OR column_name LIKE '%remoteid' ORDER BY column_name asc;" % sql.db
            if hideSQLQueries:
                print(queryGetColumn)
            cursor.execute(queryGetColumn)
            rawColumnList = cursor.fetchall()
            for i in rawColumnList:
                columnList.append(i.get("column_name").lower())
            for column in columnList:
                if "_" in column:
                    column = column.replace(column[column.rfind("_")], "")
                if "remoteid" in column:
                    if column[:-8] in tables:
                        mapping.update({column: column[:-8]})
                else:
                    if column[:-2] in tables:
                        mapping.update({column: column[:-2]})
                    else:
                        columnsWithoutAssociateTable.append(column)
                        # print(str(datetime.datetime.now()) + " [WARN] Column " + column + " have no appropriate table...")
                        logger.warn("Column {} have no appropriate table...".format(column))
            return mapping, columnsWithoutAssociateTable
    finally:
        sql.connection.close()


def prepareQuerySections(table, mapping):
    columnString = getColumnList("prod", table)
    setColumnList = ""
    setJoinSection = ""
    tmpOrderList = []
    for column in columnString.split(","):
        if column[2:] in list(mapping):
            if "remoteid" in column[2:]:
                if "remoteid" in getColumnList("prod", column[2:-8]):
                    setColumnList = setColumnList + mapping.get(column[2:]) + ".remoteid as " + column[2:] + ","
                else:
                    setColumnList = setColumnList + mapping.get(column[2:]) + ".id as " + column[2:] + ","
            elif "id" in column[2:]:
                if "remoteid" in getColumnList("prod", column[2:-2]):
                    setColumnList = setColumnList + mapping.get(column[2:]) + ".remoteid as " + column[2:] + ","
                else:
                    setColumnList = setColumnList + mapping.get(column[2:]) + ".id as " + column[2:] + ","
            else:
                if "remoteid" in getColumnList("prod", column[2:]):
                    setColumnList = setColumnList + mapping.get(column[2:]) + ".remoteid as " + column[2:] + ","
                else:
                    setColumnList = setColumnList + mapping.get(column[2:]) + ".id as " + column[2:] + ","
            setJoinSection = setJoinSection + "JOIN " + mapping.get(column[2:]) + " ON t." + column[2:] + "=" + mapping.get(column[2:]) + ".id "
        else:
            setColumnList = setColumnList + column + ","
    if setColumnList[-1:] == ",":
        setColumnList = setColumnList[:-1]
    for i in setColumnList.split(","):
        if " as " in i:
            tmpOrderList.append(i[i.rfind(" "):])
        else:
            tmpOrderList.append(i)
    setOrderList = []
    if "t.dt" in tmpOrderList:
        setOrderList.append("t.dt")
    if "campaignid" in tmpOrderList:
        setOrderList.append("campaignid")
    for item in tmpOrderList:
        if "id" in item and "campaignid" not in item:
            setOrderList.append(item)
    for item in tmpOrderList:
        if item not in tmpOrderList:
            setOrderList.append(item)
    columns = ",".join(setOrderList)
    return columnString, setColumnList, setJoinSection, columns


def prepareTableList(tables, tablesWithDifferentSchema):
    for table in excludedTables:
        if table in tables:
            tables.remove(table)
    if tablesWithDifferentSchema is not None:
        for table in tablesWithDifferentSchema:
            if table in tables:
                tables.remove(table)
    try:
        if len(clientIgnoreTables) > 0:
            for table in clientIgnoreTables.split(","):
                if table in tables:
                    tables.remove(table)
    except configparser.NoOptionError:
        # print(str(datetime.datetime.now()) + "[WARN] Property " + client + ".ignoreTables in section [specificIgnoredTables] absent")
        logger.warn("Property {}.ignoreTables in section [specificIgnoredTables] absent.".format(client))
    return tables


def prepareToTest(client):
    createTestDir("/mxf/data/test_results/", client)
    startTime = datetime.datetime.now()
    # print(str(datetime.datetime.now()) + " [INFO] Start " + client + " processing!\n")
    logger.info("Start {} processing!\n".format(client))
    tables = converters.convertToList(compareTableLists())
    return startTime, tables


def queryEntityConstructor(table, threshold, comparingStep, mapping):
    queryList = []
    columnString, setColumnList, setJoinSection, setOrderList = prepareQuerySections(table, mapping)
    offset = 0
    query = "SELECT %s FROM %s AS t" % (setColumnList, table)
    if setJoinSection:
        query = query + " %s" % setJoinSection
    if setOrderList:
        query = query + " ORDER BY %s" % setOrderList
    if threshold > comparingStep:
        while offset < threshold:
            offset = offset + comparingStep
            queryWithLimit = query + " LIMIT %d,%d;" % (offset, comparingStep)
            queryList.append(queryWithLimit)
    else:
        queryList.append(query + ";")
    return queryList


def queryReportConstruct(table, dt, mode, threshold, comparingStep, mapping):
    queryList = []
    if mode == "day-sum":
        query = "SELECT SUM(IMPRESSIONS), SUM(CLICKS) FROM %s WHERE dt = '%s';" % (table, dt)
        queryList.append(query)
    elif mode == "section-sum":
        # TODO: add code for comparing by some sections
        sections = []  # Sections for imp-aggregating
        columnString, setColumnList, setJoinSection, setOrderList = prepareQuerySections(table, mapping)
        for column in columnString.split(","):
            if "id" == column[-2:]:
                sections.append(column)
                columnListWithSums = []
                for item in setColumnList.split(","):
                    if "clicks" in item or "impressions" in item:
                        columnListWithSums.append("sum(" + item + ")")
                    elif " as " in item:
                        columnListWithSums.append(item[item.rfind(" "):])
                    else:
                        columnListWithSums.append(item)
                query = "SELECT %s FROM %s as t %s WHERE t.dt = '%s' group by %s;" % (",".join(columnListWithSums), table, setJoinSection, dt, column)
                queryList.append(query)
    elif mode == "detailed":
        offset = 0
        while offset < threshold:
            columnString, setColumnList, setJoinSection, setOrderList = prepareQuerySections(table, mapping)
            query = "SELECT %s FROM %s AS t %s WHERE t.dt>='%s' ORDER BY %s LIMIT %d,%d;" % (setColumnList, table, setJoinSection, dt, setOrderList, offset, comparingStep)
            offset = offset + comparingStep
            queryList.append(query)
    else:
        # print(str(datetime.datetime.now()) + " [ERROR] Property reportCheckType has incorrect value " + mode + ". Please, set any of this value: day-sum, section-sum, detailed.")
        logger.error("Property reportCheckType has incorrect value {}. Please, set any of this value: day-sum, section-sum, detailed.".format(mode))
        sys.exit(1)
    return queryList


def writeUniqueEntitiesToFile(table, listUniqs, stage):
    # print(str(datetime.datetime.now()) + " [ERROR] There are " + str(len(listUniqs)) + " unique elements in table " + table + " on " + stage + "-server. Detailed list of records saved to /tmp/" + table + "_uniqRecords_" + stage)
    logger.error("There are {0} unique elements in table {1} on {2}-server. Detailed list of records saved to /tmp/{1}_uniqRecords_{2}".format(len(listUniqs), table, stage))
    with open("/tmp/" + table + "_uniqueRecords_" + stage, "w") as file:
        firstList = converters.convertToList(listUniqs)
        firstList.sort()
        for item in firstList:
            file.write(item)


for client in config.getClients():
    clientConfig = configHelper.ifmsConfigClient(propertyFile, client)
    sqlPropertyDict = clientConfig.getSQLConnectParams()
    clientIgnoreTables = config.getProperty("specificIgnoredTables", client + ".ignoreTables")
    noCrossedDatesTables = []
    columnsWithoutAssociateTable = []
    emptyTables = []
    differingTables = []
    emptyProdTables = emptyTestTables = []
    prodUniqueTables = testUniqueTables = set()
    globalBreak = False
    startTime, tables = prepareToTest(client)
    if enableSchemaChecking:
        tablesWithDifferentSchema = compareTablesMetadata(tables)
        if not tablesWithDifferentSchema and failWithFirstError:
            schemaComparingTime = str(datetime.datetime.now() - startTime)
            # print(str(datetime.datetime.now()) + " [INFO] Schema partially compared in " + schemaComparingTime)
            logger.info("Schema partially compared in {}".format(schemaComparingTime))
        else:
            schemaComparingTime = str(datetime.datetime.now() - startTime)
            # print(str(datetime.datetime.now()) + " [INFO] Schema compared in " + schemaComparingTime)
            logger.info("Schema compared in {}".format(schemaComparingTime))
            dataComparingTime = compareData(tables, tablesWithDifferentSchema, globalBreak, noCrossedDatesTables, emptyTables, emptyProdTables, emptyTestTables, differingTables)
    else:
        # print(str(datetime.datetime.now()) + " Schema checking disabled...")
        logger.info("Schema checking disabled...")
        tablesWithDifferentSchema = []
        dataComparingTime = compareData(tables, [], globalBreak, noCrossedDatesTables, emptyTables, emptyProdTables, emptyTestTables, differingTables)
    subject = "[Test] Check databases for client %s" % client
    body = generateMailText(emptyTables, differingTables, noCrossedDatesTables, columnsWithoutAssociateTable, prodUniqueTables, testUniqueTables)
    helper.sendmail(body, sendMailFrom, sendMailTo, mailPassword, subject, None)