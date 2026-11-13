"""Monthly brief (Airflow 3): one held-BDC brief per client mandate."""
import pendulum
from airflow.sdk import dag, task


@dag(dag_id="monthly_brief_dag",
     schedule="30 7 * * 1-5",
     start_date=pendulum.datetime(2026, 10, 1, tz="America/New_York"),
     catchup=False, tags=["lantern", "cs3b"])
def monthly_brief():
    @task
    def build():
        from services.heartbeat import monthly_brief_job
        return monthly_brief_job()

    build()


monthly_brief()
