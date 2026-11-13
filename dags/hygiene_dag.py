"""Hygiene (Airflow 3): Mondays 08:00 New York time. Watchlist entries not reviewed in 30 days;
client reporting dates in the next 5 business days.

Thin by design: all logic lives in services.heartbeat.hygiene_job.
"""
import pendulum
from airflow.sdk import dag, task


@dag(dag_id="hygiene_dag",
     schedule="0 8 * * 1",                       # Mondays 08:00
     start_date=pendulum.datetime(2026, 10, 1, tz="America/New_York"),
     catchup=False, max_active_runs=1, tags=["lantern", "cs3b"])
def hygiene():
    @task
    def sweep():
        from services.heartbeat import hygiene_job
        return hygiene_job()

    sweep()


hygiene()
