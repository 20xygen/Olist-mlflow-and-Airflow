from datetime import datetime, timedelta, timezone

from airflow.sdk import dag, get_current_context, task

import olist_pipeline


TASK_TIMEOUT = timedelta(minutes=10)
EXTRACT_RETRIES = 2
EXTRACT_RETRY_DELAY = timedelta(seconds=30)


@dag(
    schedule="@weekly",
    start_date=datetime(2026, 11, 5, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={
        "execution_timeout": TASK_TIMEOUT,
    },
)
def olist_training():
    @task(
        task_id="extract",
        retries=EXTRACT_RETRIES,
        retry_delay=EXTRACT_RETRY_DELAY,
    )
    def extract_task():
        return olist_pipeline.extract(get_current_context()["run_id"])

    @task(task_id="validate", retries=0, trigger_rule="all_success")
    def validate_task(snapshot):
        return olist_pipeline.validate(snapshot)

    @task(task_id="prepare", trigger_rule="all_success")
    def prepare_task(snapshot):
        return olist_pipeline.prepare(snapshot)

    @task(task_id="train", trigger_rule="all_success")
    def train_task(prepared):
        return olist_pipeline.train(prepared, dag_run_id=get_current_context()["run_id"])

    snapshot = extract_task()
    validated_snapshot = validate_task(snapshot)
    prepared = prepare_task(validated_snapshot)
    train_task(prepared)


olist_training()
