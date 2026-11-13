"""Monthly brief (Airflow 3): 07:30 New York time, weekdays. On a release day (release_watch_dag
found the release that morning), one brief per mandate, to Priya, with everything held overnight.
On any other day the job logs an idle tick and returns.

Thin by design: all logic lives in services.heartbeat.brief_job.
"""
import pendulum
from airflow.sdk import dag, task


@dag(dag_id="monthly_brief_dag",
     schedule="30 7 * * 1-5",                    # 07:30 weekdays; the job checks for a release day
     start_date=pendulum.datetime(2026, 10, 1, tz="America/New_York"),
     catchup=False, max_active_runs=1, tags=["lantern", "cs3b"])
def monthly_brief():
    @task
    def brief():
        from services.heartbeat import brief_job
        return brief_job()

    brief()


monthly_brief()
