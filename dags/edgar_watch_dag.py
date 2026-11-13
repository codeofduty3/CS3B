"""EDGAR watch (Airflow 3): a new 10-Q, 10-K or 8-K Item 2.02 from a held BDC?

Thin by design: all logic lives in services.heartbeat.edgar_job, which pytest and the notebook run
with an injected clock and the recorded feed. Quiet hours (21:00 to 07:00) hold an alert for the brief.
"""
import pendulum
from airflow.sdk import dag, task


@dag(dag_id="edgar_watch_dag",
     schedule="*/30 * * * *",                    # every 30 minutes
     start_date=pendulum.datetime(2026, 10, 1, tz="America/New_York"),
     catchup=False, max_active_runs=1, tags=["lantern", "cs3b"])
def edgar_watch():
    @task
    def tick():
        from services.heartbeat import edgar_job
        return edgar_job()

    tick()


edgar_watch()
