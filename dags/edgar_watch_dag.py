"""Edgar watch (Airflow 3): new 10-Q, 10-K or material 8-K filings."""
import pendulum
from airflow.sdk import dag, task


@dag(dag_id="edgar_watch_dag",
     schedule="*/30 * * * *",
     start_date=pendulum.datetime(2026, 10, 1, tz="America/New_York"),
     catchup=False, tags=["lantern", "cs3b"])
def edgar_watch():
    @task
    def poll():
        from services.heartbeat import edgar_job, state_path
        return edgar_job(state_file=state_path("edgar"))

    poll()


edgar_watch()
