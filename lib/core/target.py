#!/usr/bin/env python

"""
Copyright (c) 2006-2013 sqlmap developers (http://sqlmap.org/)
See the file 'doc/COPYING' for copying permission
"""

import codecs
import functools
import os
import re
import tempfile
import time
import urlparse

from lib.core.common import Backend
from lib.core.common import hashDBRetrieve
from lib.core.common import intersect
from lib.core.common import paramToDict
from lib.core.common import readInput
from lib.core.common import resetCookieJar
from lib.core.common import urldecode
from lib.core.data import cmdLineOptions
from lib.core.data import conf
from lib.core.data import kb
from lib.core.data import logger
from lib.core.data import paths
from lib.core.dicts import DBMS_DICT
from lib.core.dump import dumper
from lib.core.enums import HASHDB_KEYS
from lib.core.enums import HTTP_HEADER
from lib.core.enums import HTTPMETHOD
from lib.core.enums import PLACE
from lib.core.enums import POST_HINT
from lib.core.exception import SqlmapFilePathException
from lib.core.exception import SqlmapGenericException
from lib.core.exception import SqlmapMissingPrivileges
from lib.core.exception import SqlmapSyntaxException
from lib.core.exception import SqlmapUserQuitException
from lib.core.option import _setDBMS
from lib.core.option import _setKnowledgeBaseAttributes
from lib.core.option import _setAuthCred
from lib.core.settings import ASTERISK_MARKER
from lib.core.settings import CUSTOM_INJECTION_MARK_CHAR
from lib.core.settings import HOST_ALIASES
from lib.core.settings import JSON_RECOGNITION_REGEX
from lib.core.settings import MULTIPART_RECOGNITION_REGEX
from lib.core.settings import PROBLEMATIC_CUSTOM_INJECTION_PATTERNS
from lib.core.settings import REFERER_ALIASES
from lib.core.settings import RESULTS_FILE_FORMAT
from lib.core.settings import SOAP_RECOGNITION_REGEX
from lib.core.settings import SUPPORTED_DBMS
from lib.core.settings import UNENCODED_ORIGINAL_VALUE
from lib.core.settings import UNICODE_ENCODING
from lib.core.settings import UNKNOWN_DBMS_VERSION
from lib.core.settings import URI_INJECTABLE_REGEX
from lib.core.settings import USER_AGENT_ALIASES
from lib.utils.hashdb import HashDB
from lib.core.xmldump import dumper as xmldumper
from thirdparty.odict.odict import OrderedDict

def _setRequestParams():
    """
    Check and set the parameters and perform checks on 'data' option for
    HTTP method POST.
    """

    if conf.direct:
        conf.parameters[None] = "direct connection"
        return

    testableParameters = False

    # Perform checks on GET parameters
    if conf.parameters.get(PLACE.GET):
        parameters = conf.parameters[PLACE.GET]
        paramDict = paramToDict(PLACE.GET, parameters)

        if paramDict:
            conf.paramDict[PLACE.GET] = paramDict
            testableParameters = True

    # Perform checks on POST parameters
    if conf.method == HTTPMETHOD.POST and conf.data is None:
        errMsg = "HTTP POST method depends on HTTP data value to be posted"
        raise SqlmapSyntaxException(errMsg)

    if conf.data is not None:
        conf.method = HTTPMETHOD.POST if not conf.method or conf.method == HTTPMETHOD.GET else conf.method

        def process(match, repl):
            retVal = match.group(0)

            if not (conf.testParameter and match.group("name") not in conf.testParameter):
                retVal = repl
                while True:
                    _ = re.search(r"\\g<([^>]+)>", retVal)
                    if _:
                        retVal = retVal.replace(_.group(0), match.group(int(_.group(1)) if _.group(1).isdigit() else _.group(1)))
                    else:
                        break

            return retVal

        if re.search(JSON_RECOGNITION_REGEX, conf.data):
            message = "JSON like data found in %s data. " % conf.method
            message += "Do you want to process it? [Y/n/q] "
            test = readInput(message, default="Y")
            if test and test[0] in ("q", "Q"):
                raise SqlmapUserQuitException
            elif test[0] not in ("n", "N"):
                conf.data = conf.data.replace(CUSTOM_INJECTION_MARK_CHAR, ASTERISK_MARKER)
                conf.data = re.sub(r'("(?P<name>[^"]+)"\s*:\s*"[^"]+)"', functools.partial(process, repl=r'\g<1>%s"' % CUSTOM_INJECTION_MARK_CHAR), conf.data)
                conf.data = re.sub(r'("(?P<name>[^"]+)"\s*:\s*)(-?\d[\d\.]*\b)', functools.partial(process, repl=r'\g<0>%s' % CUSTOM_INJECTION_MARK_CHAR), conf.data)
                kb.postHint = POST_HINT.JSON

        elif re.search(SOAP_RECOGNITION_REGEX, conf.data):
            message = "SOAP/XML like data found in %s data. " % conf.method
            message += "Do you want to process it? [Y/n/q] "
            test = readInput(message, default="Y")
            if test and test[0] in ("q", "Q"):
                raise SqlmapUserQuitException
            elif test[0] not in ("n", "N"):
                conf.data = conf.data.replace(CUSTOM_INJECTION_MARK_CHAR, ASTERISK_MARKER)
                conf.data = re.sub(r"(<(?P<name>[^>]+)( [^<]*)?>)([^<]+)(</\2)", functools.partial(process, repl=r"\g<1>\g<4>%s\g<5>" % CUSTOM_INJECTION_MARK_CHAR), conf.data)
                kb.postHint = POST_HINT.SOAP if "soap" in conf.data.lower() else POST_HINT.XML

        elif re.search(MULTIPART_RECOGNITION_REGEX, conf.data):
            message = "Multipart like data found in %s data. " % conf.method
            message += "Do you want to process it? [Y/n/q] "
            test = readInput(message, default="Y")
            if test and test[0] in ("q", "Q"):
                raise SqlmapUserQuitException
            elif test[0] not in ("n", "N"):
                conf.data = conf.data.replace(CUSTOM_INJECTION_MARK_CHAR, ASTERISK_MARKER)
                conf.data = re.sub(r"(?si)(Content-Disposition.+?)((\r)?\n--)", r"\g<1>%s\g<2>" % CUSTOM_INJECTION_MARK_CHAR, conf.data)
                kb.postHint = POST_HINT.MULTIPART

        if not kb.postHint:
            if CUSTOM_INJECTION_MARK_CHAR in conf.data:  # later processed
                pass
            else:
                place = PLACE.POST

                conf.parameters[place] = conf.data
                paramDict = paramToDict(place, conf.data)

                if paramDict:
                    conf.paramDict[place] = paramDict
                    testableParameters = True
        else:
            if CUSTOM_INJECTION_MARK_CHAR not in conf.data:  # in case that no usable parameter values has been found
                conf.parameters[PLACE.POST] = conf.data

    kb.processUserMarks = True if (kb.postHint and CUSTOM_INJECTION_MARK_CHAR in conf.data) else kb.processUserMarks

    if re.search(URI_INJECTABLE_REGEX, conf.url, re.I) and not any(place in conf.parameters for place in (PLACE.GET, PLACE.POST)) and not kb.postHint:
        warnMsg = "you've provided target URL without any GET "
        warnMsg += "parameters (e.g. www.site.com/article.php?id=1) "
        warnMsg += "and without providing any POST parameters "
        warnMsg += "through --data option"
        logger.warn(warnMsg)

        message = "do you want to try URI injections "
        message += "in the target URL itself? [Y/n/q] "
        test = readInput(message, default="Y")

        if not test or test[0] not in ("n", "N"):
            conf.url = "%s%s" % (conf.url, CUSTOM_INJECTION_MARK_CHAR)
            kb.processUserMarks = True
        elif test[0] in ("q", "Q"):
            raise SqlmapUserQuitException

    for place, value in ((PLACE.URI, conf.url), (PLACE.CUSTOM_POST, conf.data), (PLACE.CUSTOM_HEADER, str(conf.httpHeaders))):
        _ = re.sub(PROBLEMATIC_CUSTOM_INJECTION_PATTERNS, "", value or "") if place == PLACE.CUSTOM_HEADER else value or ""
        if CUSTOM_INJECTION_MARK_CHAR in _:
            if kb.processUserMarks is None:
                lut = {PLACE.URI: '-u', PLACE.CUSTOM_POST: '--data', PLACE.CUSTOM_HEADER: '--headers/--user-agent/--referer/--cookie'}
                message = "custom injection marking character ('%s') found in option " % CUSTOM_INJECTION_MARK_CHAR
                message += "'%s'. Do you want to process it? [Y/n/q] " % lut[place]
                test = readInput(message, default="Y")
                if test and test[0] in ("q", "Q"):
                    raise SqlmapUserQuitException
                else:
                    kb.processUserMarks = not test or test[0] not in ("n", "N")

            if not kb.processUserMarks:
                if place == PLACE.URI:
                    query = urlparse.urlsplit(value).query
                    if query:
                        parameters = conf.parameters[PLACE.GET] = query
                        paramDict = paramToDict(PLACE.GET, parameters)

                        if paramDict:
                            conf.url = conf.url.split('?')[0]
                            conf.paramDict[PLACE.GET] = paramDict
                            testableParameters = True
                elif place == PLACE.CUSTOM_POST:
                    conf.parameters[PLACE.POST] = conf.data
                    paramDict = paramToDict(PLACE.POST, conf.data)

                    if paramDict:
                        conf.paramDict[PLACE.POST] = paramDict
                        testableParameters = True

            else:
                conf.parameters[place] = value
                conf.paramDict[place] = OrderedDict()

                if place == PLACE.CUSTOM_HEADER:
                    for index in xrange(len(conf.httpHeaders)):
                        header, value = conf.httpHeaders[index]
                        if CUSTOM_INJECTION_MARK_CHAR in re.sub(PROBLEMATIC_CUSTOM_INJECTION_PATTERNS, "", value):
                            parts = value.split(CUSTOM_INJECTION_MARK_CHAR)
                            for i in xrange(len(parts) - 1):
                                conf.paramDict[place]["%s #%d%s" % (header, i + 1, CUSTOM_INJECTION_MARK_CHAR)] = "%s,%s" % (header, "".join("%s%s" % (parts[j], CUSTOM_INJECTION_MARK_CHAR if i == j else "") for j in xrange(len(parts))))
                            conf.httpHeaders[index] = (header, value.replace(CUSTOM_INJECTION_MARK_CHAR, ""))
                else:
                    parts = value.split(CUSTOM_INJECTION_MARK_CHAR)

                    for i in xrange(len(parts) - 1):
                        conf.paramDict[place]["%s#%d%s" % (("%s " % kb.postHint) if kb.postHint else "", i + 1, CUSTOM_INJECTION_MARK_CHAR)] = "".join("%s%s" % (parts[j], CUSTOM_INJECTION_MARK_CHAR if i == j else "") for j in xrange(len(parts)))

                    if place == PLACE.URI and PLACE.GET in conf.paramDict:
                        del conf.paramDict[PLACE.GET]
                    elif place == PLACE.CUSTOM_POST and PLACE.POST in conf.paramDict:
                        del conf.paramDict[PLACE.POST]

                testableParameters = True

    if kb.processUserMarks:
        for item in ("url", "data", "agent", "referer", "cookie"):
            if conf.get(item):
                conf[item] = conf[item].replace(CUSTOM_INJECTION_MARK_CHAR, "")

    # Perform checks on Cookie parameters
    if conf.cookie:
        conf.parameters[PLACE.COOKIE] = conf.cookie
        paramDict = paramToDict(PLACE.COOKIE, conf.cookie)

        if paramDict:
            conf.paramDict[PLACE.COOKIE] = paramDict
            testableParameters = True

    # Perform checks on header values
    if conf.httpHeaders:
        for httpHeader, headerValue in conf.httpHeaders:
            # Url encoding of the header values should be avoided
            # Reference: http://stackoverflow.com/questions/5085904/is-ok-to-urlencode-the-value-in-headerlocation-value

            httpHeader = httpHeader.title()

            if httpHeader == HTTP_HEADER.USER_AGENT:
                conf.parameters[PLACE.USER_AGENT] = urldecode(headerValue)

                condition = any((not conf.testParameter, intersect(conf.testParameter, USER_AGENT_ALIASES)))

                if condition:
                    conf.paramDict[PLACE.USER_AGENT] = {PLACE.USER_AGENT: headerValue}
                    testableParameters = True

            elif httpHeader == HTTP_HEADER.REFERER:
                conf.parameters[PLACE.REFERER] = urldecode(headerValue)

                condition = any((not conf.testParameter, intersect(conf.testParameter, REFERER_ALIASES)))

                if condition:
                    conf.paramDict[PLACE.REFERER] = {PLACE.REFERER: headerValue}
                    testableParameters = True

            elif httpHeader == HTTP_HEADER.HOST:
                conf.parameters[PLACE.HOST] = urldecode(headerValue)

                condition = any((not conf.testParameter, intersect(conf.testParameter, HOST_ALIASES)))

                if condition:
                    conf.paramDict[PLACE.HOST] = {PLACE.HOST: headerValue}
                    testableParameters = True

    if not conf.parameters:
        errMsg = "you did not provide any GET, POST and Cookie "
        errMsg += "parameter, neither an User-Agent, Referer or Host header value"
        raise SqlmapGenericException(errMsg)

    elif not testableParameters:
        errMsg = "all testable parameters you provided are not present "
        errMsg += "within the given request data"
        raise SqlmapGenericException(errMsg)

def _setHashDB():
    """
    Check and set the HashDB SQLite file for query resume functionality.
    """

    if not conf.hashDBFile:
        conf.hashDBFile = conf.sessionFile or "%s%ssession.sqlite" % (conf.outputPath, os.sep)

    if os.path.exists(conf.hashDBFile):
        if conf.flushSession:
            try:
                os.remove(conf.hashDBFile)
                logger.info("flushing session file")
            except OSError, msg:
                errMsg = "unable to flush the session file (%s)" % msg
                raise SqlmapFilePathException(errMsg)

    conf.hashDB = HashDB(conf.hashDBFile)

def _resumeHashDBValues():
    """
    Resume stored data values from HashDB
    """

    kb.absFilePaths = hashDBRetrieve(HASHDB_KEYS.KB_ABS_FILE_PATHS, True) or kb.absFilePaths
    kb.chars = hashDBRetrieve(HASHDB_KEYS.KB_CHARS, True) or kb.chars
    kb.dynamicMarkings = hashDBRetrieve(HASHDB_KEYS.KB_DYNAMIC_MARKINGS, True) or kb.dynamicMarkings
    kb.brute.tables = hashDBRetrieve(HASHDB_KEYS.KB_BRUTE_TABLES, True) or kb.brute.tables
    kb.brute.columns = hashDBRetrieve(HASHDB_KEYS.KB_BRUTE_COLUMNS, True) or kb.brute.columns
    kb.xpCmdshellAvailable = hashDBRetrieve(HASHDB_KEYS.KB_XP_CMDSHELL_AVAILABLE) or kb.xpCmdshellAvailable

    conf.tmpPath = conf.tmpPath or hashDBRetrieve(HASHDB_KEYS.CONF_TMP_PATH)

    for injection in hashDBRetrieve(HASHDB_KEYS.KB_INJECTIONS, True) or []:
        if injection.place in conf.paramDict and \
            injection.parameter in conf.paramDict[injection.place]:

            if not conf.tech or intersect(conf.tech, injection.data.keys()):
                if intersect(conf.tech, injection.data.keys()):
                    injection.data = dict(filter(lambda (key, item): key in conf.tech, injection.data.items()))

                if injection not in kb.injections:
                    kb.injections.append(injection)

    _resumeDBMS()
    _resumeOS()

def _resumeDBMS():
    """
    Resume stored DBMS information from HashDB
    """

    value = hashDBRetrieve(HASHDB_KEYS.DBMS)

    if not value:
        return

    dbms = value.lower()
    dbmsVersion = [UNKNOWN_DBMS_VERSION]
    _ = "(%s)" % ("|".join([alias for alias in SUPPORTED_DBMS]))
    _ = re.search("%s ([\d\.]+)" % _, dbms, re.I)

    if _:
        dbms = _.group(1).lower()
        dbmsVersion = [_.group(2)]

    if conf.dbms:
        check = True
        for aliases, _, _ in DBMS_DICT.values():
            if conf.dbms.lower() in aliases and dbms not in aliases:
                check = False
                break

        if not check:
            message = "you provided '%s' as a back-end DBMS, " % conf.dbms
            message += "but from a past scan information on the target URL "
            message += "sqlmap assumes the back-end DBMS is '%s'. " % dbms
            message += "Do you really want to force the back-end "
            message += "DBMS value? [y/N] "
            test = readInput(message, default="N")

            if not test or test[0] in ("n", "N"):
                conf.dbms = None
                Backend.setDbms(dbms)
                Backend.setVersionList(dbmsVersion)
    else:
        infoMsg = "resuming back-end DBMS '%s' " % dbms
        logger.info(infoMsg)

        Backend.setDbms(dbms)
        Backend.setVersionList(dbmsVersion)

def _resumeOS():
    """
    Resume stored OS information from HashDB
    """

    value = hashDBRetrieve(HASHDB_KEYS.OS)

    if not value:
        return

    os = value

    if os and os != 'None':
        infoMsg = "resuming back-end DBMS operating system '%s' " % os
        logger.info(infoMsg)

        if conf.os and conf.os.lower() != os.lower():
            message = "you provided '%s' as back-end DBMS operating " % conf.os
            message += "system, but from a past scan information on the "
            message += "target URL sqlmap assumes the back-end DBMS "
            message += "operating system is %s. " % os
            message += "Do you really want to force the back-end DBMS "
            message += "OS value? [y/N] "
            test = readInput(message, default="N")

            if not test or test[0] in ("n", "N"):
                conf.os = os
        else:
            conf.os = os

        Backend.setOs(conf.os)

def _setResultsFile():
    """
    Create results file for storing results of running in a
    multiple target mode.
    """

    if not conf.multipleTargets:
        return

    if not conf.resultsFP:
        conf.resultsFilename = "%s%s%s" % (paths.SQLMAP_OUTPUT_PATH, os.sep, time.strftime(RESULTS_FILE_FORMAT).lower())
        conf.resultsFP = codecs.open(conf.resultsFilename, "w+", UNICODE_ENCODING, buffering=0)
        conf.resultsFP.writelines("Target URL,Place,Parameter,Techniques%s" % os.linesep)

        logger.info("using '%s' as the CSV results file in multiple targets mode" % conf.resultsFilename)

def _createFilesDir():
    """
    Create the file directory.
    """

    if not conf.rFile:
        return

    conf.filePath = paths.SQLMAP_FILES_PATH % conf.hostname

    if not os.path.isdir(conf.filePath):
        os.makedirs(conf.filePath, 0755)

def _createDumpDir():
    """
    Create the dump directory.
    """

    if not conf.dumpTable and not conf.dumpAll and not conf.search:
        return

    conf.dumpPath = paths.SQLMAP_DUMP_PATH % conf.hostname

    if not os.path.isdir(conf.dumpPath):
        os.makedirs(conf.dumpPath, 0755)

def _configureDumper():
    if hasattr(conf, 'xmlFile') and conf.xmlFile:
        conf.dumper = xmldumper
    else:
        conf.dumper = dumper

    conf.dumper.setOutputFile()

def _createTargetDirs():
    """
    Create the output directory.
    """

    if not os.path.isdir(paths.SQLMAP_OUTPUT_PATH):
        try:
            os.makedirs(paths.SQLMAP_OUTPUT_PATH, 0755)
        except OSError, ex:
            tempDir = tempfile.mkdtemp(prefix="sqlmapoutput")
            warnMsg = "unable to create default root output directory "
            warnMsg += "'%s' (%s). " % (paths.SQLMAP_OUTPUT_PATH, ex)
            warnMsg += "using temporary directory '%s' instead" % tempDir
            logger.warn(warnMsg)

            paths.SQLMAP_OUTPUT_PATH = tempDir

    conf.outputPath = "%s%s%s" % (paths.SQLMAP_OUTPUT_PATH, os.sep, conf.hostname)

    if not os.path.isdir(conf.outputPath):
        try:
            os.makedirs(conf.outputPath, 0755)
        except OSError, ex:
            tempDir = tempfile.mkdtemp(prefix="sqlmapoutput")
            warnMsg = "unable to create output directory "
            warnMsg += "'%s' (%s). " % (conf.outputPath, ex)
            warnMsg += "using temporary directory '%s' instead" % tempDir
            logger.warn(warnMsg)

            conf.outputPath = tempDir

    try:
        with codecs.open(os.path.join(conf.outputPath, "target.txt"), "w+", UNICODE_ENCODING) as f:
            f.write(kb.originalUrls.get(conf.url) or conf.url or conf.hostname)
            f.write(" (%s)" % (HTTPMETHOD.POST if conf.data else HTTPMETHOD.GET))
            if conf.data:
                f.write("\n\n%s" % conf.data)
    except IOError, ex:
        if "denied" in str(ex):
            errMsg = "you don't have enough permissions "
        else:
            errMsg = "something went wrong while trying "
        errMsg += "to write to the output directory '%s' (%s)" % (paths.SQLMAP_OUTPUT_PATH, ex)

        raise SqlmapMissingPrivileges(errMsg)

    _createDumpDir()
    _createFilesDir()
    _configureDumper()

def _restoreCmdLineOptions():
    """
    Restore command line options that could be possibly
    changed during the testing of previous target.
    """

    conf.col = cmdLineOptions.col
    conf.db = cmdLineOptions.db
    conf.dnsName = cmdLineOptions.dnsName
    conf.privEsc = cmdLineOptions.privEsc
    conf.tbl = cmdLineOptions.tbl
    conf.regexp = cmdLineOptions.regexp
    conf.string = cmdLineOptions.string
    conf.textOnly = cmdLineOptions.textOnly
    conf.threads = cmdLineOptions.threads
    conf.timeSec = cmdLineOptions.timeSec
    conf.tmpPath = cmdLineOptions.tmpPath
    conf.uChar = cmdLineOptions.uChar
    conf.user = cmdLineOptions.user

def initTargetEnv():
    """
    Initialize target environment.
    """

    if conf.multipleTargets:
        if conf.hashDB:
            conf.hashDB.close()

        if conf.cj:
            resetCookieJar(conf.cj)

        conf.paramDict = {}
        conf.parameters = {}
        conf.hashDBFile = None

        _setKnowledgeBaseAttributes(False)
        _restoreCmdLineOptions()
        _setDBMS()

    if conf.data:
        class _(unicode):
            pass

        original = conf.data
        conf.data = _(urldecode(conf.data))
        setattr(conf.data, UNENCODED_ORIGINAL_VALUE, original)

        kb.postSpaceToPlus = '+' in original

def setupTargetEnv():
    _createTargetDirs()
    _setRequestParams()
    _setHashDB()
    _resumeHashDBValues()
    _setResultsFile()
    _setAuthCred()
