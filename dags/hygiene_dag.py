"""Hygiene (Airflow 3): stale watchlists and upcoming client reporting."""
import pendulum
from airflow.sdk import dag, task


@dag(dag_id="hygiene_dag",
     schedule="0 8 * * 1",
     start_date=pendulum.datetime(2026, 10, 1, tz="America/New_York"),
     catchup=False, tags=["lantern", "cs3b"])
def hygiene():
    @task
    def review():
        from services.heartbeat import hygiene_job
        return hygiene_job()

    review()


hygiene()
