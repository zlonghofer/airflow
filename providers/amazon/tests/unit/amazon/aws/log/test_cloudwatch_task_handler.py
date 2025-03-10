#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import logging
import time
from datetime import datetime as dt, timedelta, timezone
from unittest import mock
from unittest.mock import ANY, Mock, call

import boto3
import pytest
from moto import mock_aws
from watchtower import CloudWatchLogHandler

from airflow.models import DAG, DagRun, TaskInstance
from airflow.providers.amazon.aws.hooks.logs import AwsLogsHook
from airflow.providers.amazon.aws.log.cloudwatch_task_handler import (
    CloudwatchTaskHandler,
)
from airflow.providers.amazon.aws.utils import datetime_to_epoch_utc_ms
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.utils.state import State
from airflow.utils.timezone import datetime

from tests_common.test_utils.config import conf_vars
from tests_common.test_utils.version_compat import AIRFLOW_V_3_0_PLUS


def get_time_str(time_in_milliseconds):
    dt_time = dt.fromtimestamp(time_in_milliseconds / 1000.0, tz=timezone.utc)
    return dt_time.strftime("%Y-%m-%d %H:%M:%S,000")


@pytest.fixture(autouse=True)
def logmock():
    with mock_aws():
        yield


@pytest.mark.db_test
class TestCloudwatchTaskHandler:
    @conf_vars({("logging", "remote_log_conn_id"): "aws_default"})
    @pytest.fixture(autouse=True)
    def setup_tests(self, create_log_template, tmp_path_factory, session):
        self.remote_log_group = "log_group_name"
        self.region_name = "us-west-2"
        self.local_log_location = str(tmp_path_factory.mktemp("local-cloudwatch-log-location"))
        if AIRFLOW_V_3_0_PLUS:
            create_log_template("{dag_id}/{task_id}/{logical_date}/{try_number}.log")
        else:
            create_log_template("{dag_id}/{task_id}/{execution_date}/{try_number}.log")
        self.cloudwatch_task_handler = CloudwatchTaskHandler(
            self.local_log_location,
            f"arn:aws:logs:{self.region_name}:11111111:log-group:{self.remote_log_group}",
        )

        date = datetime(2020, 1, 1)
        dag_id = "dag_for_testing_cloudwatch_task_handler"
        task_id = "task_for_testing_cloudwatch_log_handler"
        self.dag = DAG(dag_id=dag_id, schedule=None, start_date=date)
        task = EmptyOperator(task_id=task_id, dag=self.dag)
        if AIRFLOW_V_3_0_PLUS:
            dag_run = DagRun(
                dag_id=self.dag.dag_id,
                logical_date=date,
                run_id="test",
                run_type="scheduled",
            )
        else:
            dag_run = DagRun(
                dag_id=self.dag.dag_id,
                execution_date=date,
                run_id="test",
                run_type="scheduled",
            )
        session.add(dag_run)
        session.commit()
        session.refresh(dag_run)

        self.ti = TaskInstance(task=task, run_id=dag_run.run_id)
        self.ti.dag_run = dag_run
        self.ti.try_number = 1
        self.ti.state = State.RUNNING
        session.add(self.ti)
        session.commit()

        self.remote_log_stream = (f"{dag_id}/{task_id}/{date.isoformat()}/{self.ti.try_number}.log").replace(
            ":", "_"
        )
        self.conn = boto3.client("logs", region_name=self.region_name)

        yield

        self.cloudwatch_task_handler.handler = None

    def test_hook(self):
        assert isinstance(self.cloudwatch_task_handler.hook, AwsLogsHook)

    def test_handler(self):
        self.cloudwatch_task_handler.set_context(self.ti)
        assert isinstance(self.cloudwatch_task_handler.handler, CloudWatchLogHandler)

    def test_write(self):
        handler = self.cloudwatch_task_handler
        handler.set_context(self.ti)
        messages = [str(i) for i in range(10)]

        with mock.patch("watchtower.CloudWatchLogHandler.emit") as mock_emit:
            for message in messages:
                handler.handle(message)
            mock_emit.assert_has_calls([call(message) for message in messages])

    def test_event_to_str(self):
        handler = self.cloudwatch_task_handler
        current_time = int(time.time()) * 1000
        events = [
            {"timestamp": current_time - 2000, "message": "First"},
            {"timestamp": current_time - 1000, "message": "Second"},
            {"timestamp": current_time, "message": "Third"},
        ]
        assert [handler._event_to_str(event) for event in events] == (
            [
                f"[{get_time_str(current_time - 2000)}] First",
                f"[{get_time_str(current_time - 1000)}] Second",
                f"[{get_time_str(current_time)}] Third",
            ]
        )

    def test_read(self):
        # Confirmed via AWS Support call:
        # CloudWatch events must be ordered chronologically otherwise
        # boto3 put_log_event API throws InvalidParameterException
        # (moto does not throw this exception)
        current_time = int(time.time()) * 1000
        generate_log_events(
            self.conn,
            self.remote_log_group,
            self.remote_log_stream,
            [
                {"timestamp": current_time - 2000, "message": "First"},
                {"timestamp": current_time - 1000, "message": "Second"},
                {"timestamp": current_time, "message": "Third"},
            ],
        )

        msg_template = "*** Reading remote log from Cloudwatch log_group: {} log_stream: {}.\n{}\n"
        events = "\n".join(
            [
                f"[{get_time_str(current_time - 2000)}] First",
                f"[{get_time_str(current_time - 1000)}] Second",
                f"[{get_time_str(current_time)}] Third",
            ]
        )
        if AIRFLOW_V_3_0_PLUS:
            assert self.cloudwatch_task_handler.read(self.ti) == (
                msg_template.format(self.remote_log_group, self.remote_log_stream, events),
                {"end_of_log": True},
            )
        else:
            assert self.cloudwatch_task_handler.read(self.ti) == (
                [
                    [
                        (
                            "",
                            msg_template.format(self.remote_log_group, self.remote_log_stream, events),
                        )
                    ]
                ],
                [{"end_of_log": True}],
            )

    @pytest.mark.parametrize(
        "end_date, expected_end_time",
        [
            (None, None),
            (
                datetime(2020, 1, 2),
                datetime_to_epoch_utc_ms(datetime(2020, 1, 2) + timedelta(seconds=30)),
            ),
        ],
    )
    @mock.patch.object(AwsLogsHook, "get_log_events")
    def test_get_cloudwatch_logs(self, mock_get_log_events, end_date, expected_end_time):
        self.ti.end_date = end_date
        self.cloudwatch_task_handler.get_cloudwatch_logs(self.remote_log_stream, self.ti)
        mock_get_log_events.assert_called_once_with(
            log_group=self.remote_log_group,
            log_stream_name=self.remote_log_stream,
            end_time=expected_end_time,
        )

    @pytest.mark.parametrize(
        "conf_json_serialize, expected_serialized_output",
        [
            pytest.param(
                "airflow.providers.amazon.aws.log.cloudwatch_task_handler.json_serialize_legacy",
                '{"datetime": "2023-01-01T00:00:00+00:00", "customObject": null}',
                id="json-serialize-legacy",
            ),
            pytest.param(
                "airflow.providers.amazon.aws.log.cloudwatch_task_handler.json_serialize",
                '{"datetime": "2023-01-01T00:00:00+00:00", "customObject": "SomeCustomSerialization(...)"}',
                id="json-serialize",
            ),
            pytest.param(
                None,
                '{"datetime": "2023-01-01T00:00:00+00:00", "customObject": null}',
                id="not-set",
            ),
        ],
    )
    @mock.patch.object(AwsLogsHook, "get_log_events")
    def test_write_json_logs(self, mock_get_log_events, conf_json_serialize, expected_serialized_output):
        class ToSerialize:
            def __init__(self):
                pass

            def __repr__(self):
                return "SomeCustomSerialization(...)"

        with conf_vars({("aws", "cloudwatch_task_handler_json_serializer"): conf_json_serialize}):
            handler = self.cloudwatch_task_handler
            handler.set_context(self.ti)
            message = logging.LogRecord(
                name="test_log_record",
                level=logging.DEBUG,
                pathname="fake.path",
                lineno=42,
                args=None,
                exc_info=None,
                msg={
                    "datetime": datetime(2023, 1, 1),
                    "customObject": ToSerialize(),
                },
            )
            with (
                mock.patch("watchtower.threading.Thread"),
                mock.patch("watchtower.queue.Queue") as mq,
            ):
                mock_queue = Mock()
                mq.return_value = mock_queue
                handler.handle(message)
                mock_queue.put.assert_called_once_with(
                    {"message": expected_serialized_output, "timestamp": ANY}
                )

    def test_close_prevents_duplicate_calls(self):
        with mock.patch("watchtower.CloudWatchLogHandler.close") as mock_log_handler_close:
            with mock.patch("airflow.utils.log.file_task_handler.FileTaskHandler.set_context"):
                self.cloudwatch_task_handler.set_context(self.ti)
                for _ in range(5):
                    self.cloudwatch_task_handler.close()

                mock_log_handler_close.assert_called_once()

    def test_filename_template_for_backward_compatibility(self):
        # filename_template arg support for running the latest provider on airflow 2
        CloudwatchTaskHandler(
            self.local_log_location,
            f"arn:aws:logs:{self.region_name}:11111111:log-group:{self.remote_log_group}",
            filename_template=None,
        )


def generate_log_events(conn, log_group_name, log_stream_name, log_events):
    conn.create_log_group(logGroupName=log_group_name)
    conn.create_log_stream(logGroupName=log_group_name, logStreamName=log_stream_name)
    conn.put_log_events(logGroupName=log_group_name, logStreamName=log_stream_name, logEvents=log_events)
