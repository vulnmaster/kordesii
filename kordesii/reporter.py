"""
DC3-Kordesii framework primary object used for execution of decoders and collection of metadata.
"""

import base64
import codecs
import contextlib
import hashlib
import io
import os
import logging
import shutil
import sys
import tempfile
from typing import List, Optional

import kordesii
from kordesii import decoders, logutil
from kordesii.serialization import deserialize

logger = logging.getLogger(__name__)
ascii_writer = codecs.getwriter("ascii")

# Constant fields
FIELD_DEBUG = "debug"
FIELD_STRINGS = "strings"
FIELD_FILES = "files"


class ReporterLogHandler(logging.Handler):
    """Custom logging handler used to keep backwards compatible with legacy logging mechanism."""

    def __init__(self, reporter):
        super(ReporterLogHandler, self).__init__()
        self._reporter = reporter

    def emit(self, record):
        message = self.format(record)
        if record.levelno > logging.WARNING:
            self._reporter.errors.append(message)
        # Even though reporter uses the name "debug".. This really is an INFO level debug message.
        # (Adding true DEBUG level messages would spam our console.)
        elif logging.INFO <= record.levelno <= logging.WARNING:
            if "debug" not in self._reporter.metadata:
                self._reporter.metadata["debug"] = []
            self._reporter.metadata["debug"].append(message)


class Reporter(object):
    """
    Class for doing decoder execution and metadata reporting.

    This class contains state and data about the current string decoder run, including extracted metadata,
    holding the actual sample, etc.

    Re-using an instance of this class on multiple samples is possible and should be safe, but it is not
    recommended.

    Parameters:
    :param tempdir: sets attribute
    :param disabletempcleanup: disable cleanup (deletion) of temp files

    Attributes:
    :var tempdir: directory where temporary files should be created. Files created in this directory should
        be deleted by decoder. See managed_tempdir for mwcp managed directory
    :var handle: file handle of file to parsed
    :var metadata: Dictionary containing the metadata extracted from the malware by the decoder
    :var errors: list of errors generated by framework. Generally decoders should not set these, they should
        use debug instead
    :var debug: list of debug messages generated by framework or decoders.
    :var strings: list of strings decoded by decoders.
    """

    def __init__(self, tempdir=None, disabletempcleanup=False, base64outputfiles=False):
        self.tempdir = tempdir or tempfile.gettempdir()
        self.metadata = {}
        self.errors = []
        # TODO: Remove disassembler specific details from reporter.
        self.ida_log = ""

        self._log_handler = None
        self._temp_file_name = ""
        self._managed_tempdir = ""

        self._disable_temp_cleanup = disabletempcleanup
        self._base64_output_files = base64outputfiles

        self._other_data = None
        self._deserialized_data = {}

    @property
    def other_data(self):
        """
        The data serialized in the default 'other_data' serializer.

        :return: Dict of the serialized data
        :rtype: dict
        """
        return self.get_serialized("other_data")

    def get_serialized(self, name="other_data"):
        """
        Get the data from a named serializer (that is, other than the default
        'other data' serializer).

        :param str name: The name of the serializer
        :return: Dict of the serialized data
        :rtype: dict
        """
        _deserialized_data = self._deserialized_data
        if name in _deserialized_data:
            return _deserialized_data[name]
        yml_data = self.get_file_contents("{}.yml".format(name))
        data = deserialize(yml_data)
        _deserialized_data[name] = data
        return data

    def managed_tempdir(self):
        """
        Returns the filename of a managed temporary directory. This directory will be deleted when decoder is
        finished, unless tempcleanup is disabled.
        """

        if not self._managed_tempdir:
            self._managed_tempdir = tempfile.mkdtemp(dir=self.tempdir, prefix="kordesii-managed_tempdir-")

            if self._disable_temp_cleanup:
                logger.debug("Using managed temp dir: %s" % self._managed_tempdir)

        return self._managed_tempdir

    def add_string(self, string):
        """
        Record a decoded string
        """
        fieldu = self.convert_to_unicode(FIELD_STRINGS)
        stringu = self.convert_to_unicode(string)

        if fieldu not in self.metadata:
            self.metadata[fieldu] = []

        self.metadata[fieldu].append(stringu)

    def get_strings(self) -> List[str]:
        """
        Get a list of any recorded strings.
        """
        if FIELD_STRINGS in self.metadata:
            return self.metadata[FIELD_STRINGS]

        return []

    def add_output_file(self, filename, data, description=""):
        """
        Add file and its data to metadata.
        """
        fieldu = self.convert_to_unicode(FIELD_FILES)
        filenameu = self.convert_to_unicode(filename)
        descriptionu = self.convert_to_unicode(description)
        md5 = hashlib.md5(data).hexdigest()

        if fieldu not in self.metadata:
            self.metadata[fieldu] = []

        if self._base64_output_files:
            self.metadata[fieldu].append([filenameu, descriptionu, md5, base64.b64encode(data).decode("latin1")])
        else:
            self.metadata[fieldu].append([filenameu, descriptionu, md5])

        if filenameu == u"other_data.yml":
            self.metadata["other_data"] = data.decode("latin1")

    def get_file_contents(self, filename) -> Optional[bytes]:
        """
        If the file name exists and has its contents are stored in the reporter, then take
        the base64 encoded contents, base64 decode it, and return it.
        """
        if FIELD_FILES in self.metadata:
            for entry in self.metadata[FIELD_FILES]:
                if entry[0] == filename and len(entry) == 4:
                    return base64.b64decode(entry[3])

        return None

    def run_decoder(self, name, filename=None, data=None, **run_config):
        """
        Runs specified decoder on file

        :param name: name of decoder module to run
        :param filename: file to parse
        :param data: use data as file instead of loading data from filename
        :param run_config: Extra configuration arguments to pass to kordesii.run_ida()
        """
        self.__reset()

        if not (filename or data):
            raise ValueError("filename or data must be provided.")

        if filename:
            input_file = filename
        else:
            # we were passed data buffer. Lazy initialize a temp file for this
            input_file = os.path.join(self.managed_tempdir(), hashlib.md5(data).hexdigest())
            with open(input_file, "wb") as file_object:
                file_object.write(data)

        try:
            with self.__redirect_stdout():
                found = False
                # TODO: Run all decoders within a single ida call.
                for decoder in kordesii.iter_decoders(name):
                    found = True
                    try:
                        decoder.run(input_file, self, **run_config)
                    except (Exception, SystemExit) as e:
                        logger.exception(
                            "Error running decoder {} on {}".format(decoder.full_name, os.path.basename(input_file))
                        )
                if not found:
                    logger.error("Could not find decoder with name: {}".format(name))
        finally:
            self.__cleanup()

    def convert_to_unicode(self, input_string):
        if isinstance(input_string, str):
            return input_string
        else:
            return str(input_string, encoding="utf8", errors="replace")

    def print_report(self):
        """
        Output in human readable report format
        """
        # Use sys.stdout.buffer if it exists, which is the case for Python 3 and is required
        # for writing a bytes string. Otherwise just write to whatever is at sys.stdout
        print(
            self.get_output_text(),
            # file=ascii_writer(getattr(sys.stdout, 'buffer', sys.stdout), 'backslashreplace')
        )

    def get_output_text(self):
        """
        Get data in human readable report format.
        """

        output = u""

        output += u"----Decoded Strings----\n\n"

        if FIELD_STRINGS not in self.metadata:
            output += u"No decoded strings found\n"
        else:
            for item in self.metadata[FIELD_STRINGS]:
                output += u"{}\n".format(item.encode("unicode-escape").decode())

        if FIELD_FILES in self.metadata:
            output += u"\n----Files----\n\n"
            for item in self.metadata[FIELD_FILES]:
                filename = item[0]
                output += u"{}\n".format(filename)

        if FIELD_DEBUG in self.metadata:
            output += u"\n----Debug----\n\n"
            for item in self.metadata[FIELD_DEBUG]:
                output += u"{}\n".format(item)

        if self.ida_log:
            output += u"\n----IDA Log----\n\n"
            output += u"{}\n".format(self.ida_log)

        if self.errors:
            output += u"\n----Errors----\n\n"
            for item in self.errors:
                output += u"{}\n".format(item)

        return output

    @contextlib.contextmanager
    def __redirect_stdout(self):
        """Redirects stdout temporarily while in a with statement."""
        debug_stdout = io.StringIO()
        orig_stdout = sys.stdout
        sys.stdout = debug_stdout
        try:
            yield
        finally:
            for line in debug_stdout.getvalue().splitlines():
                logger.debug(line)
            sys.stdout = orig_stdout

    def __reset(self):
        """
        Reset all the data in the reporter object that is set during the run_decoder function

        Goal is to make the reporter safe to use for multiple run_decoder instances
        """
        self._temp_file_name = ""
        self._managed_tempdir = ""

        self.metadata = {}
        self.errors = []
        self.ida_log = ""

        # To keep backwards compatibility, setup log handler to add errors and debug messages to reporter.
        # TODO: Remove this when the Reporter object should no longer be responsible for logging.
        log_handler = ReporterLogHandler(self)
        logging.root.addHandler(log_handler)
        # Setup a simple format that doesn't contain any runtime variables.
        log_handler.addFilter(logutil.LevelCharFilter())
        log_handler.setFormatter(logging.Formatter("[%(level_char)s] %(message)s"))
        self._log_handler = log_handler

    def __cleanup(self):
        """
        Cleanup things
        """
        # Remove log handler.
        if self._log_handler:
            logging.root.removeHandler(self._log_handler)
            self._log_handler = None

        # Delete temporary directory.
        if not self._disable_temp_cleanup:
            if self._temp_file_name:
                try:
                    os.remove(self._temp_file_name)
                except Exception as e:
                    logger.warning("Failed to purge temp file: %s, %s" % (self._temp_file_name, str(e)))
                self._temp_file_name = ""

            if self._managed_tempdir:
                try:
                    shutil.rmtree(self._managed_tempdir, ignore_errors=True)
                except Exception as e:
                    logger.warning("Failed to purge temp dir: %s, %s" % (self._managed_tempdir, str(e)))
                self._managed_tempdir = ""

        self._temp_file_name = ""
        self._managed_tempdir = ""

    def __del__(self):
        self.__cleanup()
