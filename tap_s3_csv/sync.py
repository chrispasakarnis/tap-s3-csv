import importlib
import sys
import csv

from singer import metadata
from singer import Transformer
from singer import utils

import singer
from singer_encodings import csv as singer_encodings_csv
from tap_s3_csv import s3

LOGGER = singer.get_logger()


def sync_stream(config, state, table_spec, stream):
    table_name = table_spec["table_name"]
    modified_since = utils.strptime_with_tz(
        singer.get_bookmark(state, table_name, "modified_since") or config["start_date"]
    )

    LOGGER.info('Syncing table "%s".', table_name)
    LOGGER.info("Getting files modified since %s.", modified_since)

    s3_files = s3.get_input_files_for_table(config, table_spec, modified_since)

    records_streamed = 0

    # We sort here so that tracking the modified_since bookmark makes
    # sense. This means that we can't sync s3 buckets that are larger than
    # we can sort in memory which is suboptimal. If we could bookmark
    # based on anything else then we could just sync files as we see them.
    for s3_file in sorted(s3_files, key=lambda item: item["last_modified"]):
        records_streamed += sync_table_file(
            config, s3_file["key"], table_spec, stream, s3_file["last_modified"]
        )

        state = singer.write_bookmark(
            state, table_name, "modified_since", s3_file["last_modified"].isoformat()
        )
        singer.write_state(state)

    LOGGER.info('Wrote %s records for table "%s".', records_streamed, table_name)

    return records_streamed


def sync_table_file(config, s3_path, table_spec, stream, last_modified):
    LOGGER.info('Syncing file "%s".', s3_path)

    bucket = config["bucket"]
    table_name = table_spec["table_name"]

    s3_file_handle = s3.get_file_handle(config, s3_path)
    # We observed data who's field size exceeded the default maximum of
    # 131072. We believe the primary consequence of the following setting
    # is that a malformed, wide CSV would potentially parse into a single
    # large field rather than giving this error, but we also think the
    # chances of that are very small and at any rate the source data would
    # need to be fixed. The other consequence of this could be larger
    # memory consumption but that's acceptable as well.
    csv.field_size_limit(sys.maxsize)

    encoding_module = singer_encodings_csv
    if 'encoding_module' in config:
        try:
            encoding_module = importlib.import_module(
                config['encoding_module']
            )
        except ModuleNotFoundError:
            LOGGER.warning(
                f'Failed to load encoding module [{config["encoding_module"]}]. Defaulting to [singer_encodings.csv]'
            )

    iterator = encoding_module.get_row_iterator(
        s3_file_handle._raw_stream, table_spec
    )  # pylint:disable=protected-access

    records_synced = 0

    for row in iterator:
        custom_columns = {
            s3.SDC_SOURCE_BUCKET_COLUMN: bucket,
            s3.SDC_SOURCE_FILE_COLUMN: s3_path,
            # index zero, +1 for header row
            s3.SDC_SOURCE_LINENO_COLUMN: records_synced + 2,
        }
        rec = {**row, **custom_columns}

        with Transformer() as transformer:
            to_write = transformer.transform(
                rec, stream["schema"], metadata.to_map(stream["metadata"])
            )

        to_write_with_sequence = RecordMessageWithSequence(
            singer.RecordMessage(stream=table_name, record=to_write), last_modified
        )

        singer.write_message(to_write_with_sequence)
        records_synced += 1

    return records_synced


# A hacky wrapper class to add the last_modified timestamp as the sequence
class RecordMessageWithSequence:
    def __init__(self, message, last_modified):
        self.message = message
        self.last_modified = int(last_modified.timestamp())

    def asdict(self):
        result = self.message.asdict()
        result["sequence"] = self.last_modified
        return result

    def __str__(self):
        return str(self.asdict())

    def __repr__(self):
        pairs = ["{}={}".format(k, v) for k, v in self.asdict().items()]
        attrstr = ", ".join(pairs)
        return "{}({})".format(self.__class__.__name__, attrstr)
