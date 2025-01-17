#!/usr/bin/env python3

import argparse
import io
import sys
import simplejson as json
import logging
import collections

from jsonschema import validate
import singer

from oauth2client import tools
from tempfile import TemporaryFile

from google.cloud import bigquery
from google.cloud.bigquery.job import SourceFormat
from google.cloud.bigquery import Dataset, WriteDisposition, SchemaUpdateOption
from google.cloud.bigquery import SchemaField
from google.cloud.bigquery import LoadJobConfig
from google.api_core import exceptions

try:
    parser = argparse.ArgumentParser(parents=[tools.argparser])
    parser.add_argument("-c", "--config", help="Config file", required=True)
    flags = parser.parse_args()

except ImportError:
    flags = None

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logger = singer.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/bigquery.insertdata",
]
CLIENT_SECRET_FILE = "client_secret.json"
APPLICATION_NAME = "Singer BigQuery Target"

StreamMeta = collections.namedtuple(
    "StreamMeta", ["schema", "key_properties", "bookmark_properties"]
)


def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        logger.debug("Emitting state {}".format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()


def clear_dict_hook(items):
    return {k: v if v is not None else "" for k, v in items}


def define_schema(field, name):
    schema_name = name
    schema_mode = "NULLABLE"
    schema_description = None
    schema_fields = ()

    if "type" not in field and "anyOf" in field:
        for types in field["anyOf"]:
            if types["type"] == "null":
                schema_mode = "NULLABLE"
            else:
                field = types

    if isinstance(field["type"], list):
        if field["type"][0] == "null":
            schema_mode = "NULLABLE"
        else:
            schema_mode = "required"
        schema_type = field["type"][-1]
    else:
        schema_type = field["type"]
    if schema_type == "object":
        schema_type = "RECORD"
        schema_fields = tuple(build_schema(field))
    if schema_type == "array":
        schema_type = field.get("items").get("type")
        schema_mode = "REPEATED"
        if schema_type == "object":
            schema_type = "RECORD"
            schema_fields = tuple(build_schema(field.get("items")))

    if schema_type == "string":
        if "format" in field:
            if field["format"] == "date-time":
                schema_type = "timestamp"

    if schema_type == "number":
        schema_type = "FLOAT"

    return schema_name, schema_type, schema_mode, schema_description, schema_fields


def build_schema(schema):
    output_schema = []
    for key in schema["properties"].keys():

        if not (bool(schema["properties"][key])):
            # if we endup with an empty record.
            continue

        (
            schema_name,
            schema_type,
            schema_mode,
            schema_description,
            schema_fields,
        ) = define_schema(schema["properties"][key], key)
        output_schema.append(
            SchemaField(
                schema_name, schema_type, schema_mode, schema_description, schema_fields
            )
        )

    return output_schema


def persist_lines_job(
    project_id,
    dataset_id,
    lines=None,
    truncate=False,
    validate_records=True,
    allow_schema_update=False,
    ignore_unknown_fields=False,
    autodetect_schema=False,
):
    state = None
    schemas = {}
    key_properties = {}
    rows = {}
    errors = {}

    bigquery_client = bigquery.Client(project=project_id)

    for line in lines:
        try:
            msg = singer.parse_message(line)
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(line))
            raise

        if isinstance(msg, singer.RecordMessage):
            if msg.stream not in schemas:
                raise Exception(
                    "A record for stream {} was encountered before a corresponding schema".format(
                        msg.stream
                    )
                )

            schema = schemas[msg.stream]

            if validate_records:
                validate(msg.record, schema)

            # NEWLINE_DELIMITED_JSON expects literal JSON formatted data, with a newline character splitting each row.
            dat = bytes(json.dumps(msg.record, use_decimal=True) + "\n", "UTF-8")

            rows[msg.stream].write(dat)
            state = None

        elif isinstance(msg, singer.StateMessage):
            logger.debug("Setting state to {}".format(msg.value))
            state = msg.value

        elif isinstance(msg, singer.SchemaMessage):
            table = msg.stream
            schemas[table] = msg.schema
            key_properties[table] = msg.key_properties
            rows[table] = TemporaryFile(mode="w+b")
            errors[table] = None

        elif isinstance(msg, singer.ActivateVersionMessage):
            # This is experimental and won't be used yet
            pass

        else:
            raise Exception("Unrecognized message {}".format(msg))

    for table in rows.keys():
        if not rows[table].tell():
            # this means the tmp file is empty, so nothing to upload.
            continue

        table_ref = bigquery_client.dataset(dataset_id).table(table)
        load_config = LoadJobConfig()
        load_config.source_format = SourceFormat.NEWLINE_DELIMITED_JSON
        load_config.ignore_unknown_values = ignore_unknown_fields

        if autodetect_schema:
            load_config.autodetect = True
        else:
            load_config.schema = build_schema(schemas[table])

        if allow_schema_update:
            load_config.schema_update_options = [
                SchemaUpdateOption.ALLOW_FIELD_ADDITION,
                SchemaUpdateOption.ALLOW_FIELD_RELAXATION,
            ]

        if truncate:
            load_config.write_disposition = WriteDisposition.WRITE_TRUNCATE

        rows[table].seek(0)
        logger.info("loading {} to Bigquery.\n".format(table))
        load_job = bigquery_client.load_table_from_file(
            rows[table], table_ref, job_config=load_config
        )
        logger.info("loading job {}".format(load_job.job_id))
        logger.info(load_job.result())

    return state


def persist_lines_stream(
    project_id, dataset_id, lines=None, validate_records=True, allow_schema_update=False
):
    state = None
    schemas = {}
    key_properties = {}
    tables = {}
    rows = {}
    errors = {}

    bigquery_client = bigquery.Client(project=project_id)

    dataset_ref = bigquery_client.dataset(dataset_id)
    dataset = Dataset(dataset_ref)
    try:
        dataset = bigquery_client.create_dataset(Dataset(dataset_ref)) or Dataset(
            dataset_ref
        )
    except exceptions.Conflict:
        pass

    for line in lines:
        try:
            msg = singer.parse_message(line)
        except json.decoder.JSONDecodeError:
            logger.error("Unable to parse:\n{}".format(line))
            raise

        if isinstance(msg, singer.RecordMessage):
            if msg.stream not in schemas:
                raise Exception(
                    "A record for stream {} was encountered before a corresponding schema".format(
                        msg.stream
                    )
                )

            schema = schemas[msg.stream]

            if validate_records:
                validate(msg.record, schema)

            errors[msg.stream] = bigquery_client.insert_rows_json(
                tables[msg.stream], [msg.record]
            )
            rows[msg.stream] += 1

            state = None

        elif isinstance(msg, singer.StateMessage):
            logger.debug("Setting state to {}".format(msg.value))
            state = msg.value

        elif isinstance(msg, singer.SchemaMessage):
            table = msg.stream
            schemas[table] = msg.schema
            key_properties[table] = msg.key_properties
            tables[table] = bigquery.Table(
                dataset.table(table), schema=build_schema(schemas[table])
            )
            rows[table] = 0
            errors[table] = None
            try:
                tables[table] = bigquery_client.create_table(tables[table])
            except exceptions.Conflict:
                pass

        elif isinstance(msg, singer.ActivateVersionMessage):
            # This is experimental and won't be used yet
            pass

        else:
            raise Exception("Unrecognized message {}".format(msg))

    for table in errors.keys():
        if not errors[table]:
            logging.info(
                "Loaded {} row(s) into {}:{}".format(
                    rows[table], dataset_id, table, tables[table].path
                )
            )
            emit_state(state)
        else:
            logging.error("Errors:", errors[table], sep=" ")

    return state


def main():
    with open(flags.config) as input_config:
        config = json.load(input_config)

    if config.get("replication_method") == "FULL_TABLE":
        truncate = True
    else:
        truncate = False

    validate_records = config.get("validate_records", True)
    allow_schema_update = config.get("allow_schema_update", False)
    ignore_unknown_fields = config.get("ignore_unknown_fields", False)
    autodetect_schema = config.get("autodetect_schema", False)

    input_data = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

    if config.get("stream_data", True):
        state = persist_lines_stream(
            config["project_id"],
            config["dataset_id"],
            input_data,
            validate_records=validate_records,
            allow_schema_update=allow_schema_update,
        )
    else:
        state = persist_lines_job(
            config["project_id"],
            config["dataset_id"],
            input_data,
            truncate=truncate,
            validate_records=validate_records,
            allow_schema_update=allow_schema_update,
            ignore_unknown_fields=ignore_unknown_fields,
            autodetect_schema=autodetect_schema,
        )

    emit_state(state)
    logger.debug("Exiting normally")


if __name__ == "__main__":
    main()
