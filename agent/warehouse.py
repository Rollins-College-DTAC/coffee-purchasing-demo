"""Optional Snowflake copy of the silver and gold layers (Milestone 4).

After each import, the run's purchases and its imports row are written to Snowflake, and the
gold views are (re)created there, so Streamlit Cloud and GitHub Actions can share one warehouse.
Skipped, and logged as skipped, when the SNOWFLAKE_* settings are missing.
"""

import os

import snowflake.connector

REQUIRED = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
            "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS PURCHASES (
         RUN_ID STRING, FILE_NAME STRING, LOT_ID STRING, ORIGIN STRING, SUPPLIER STRING,
         QUANTITY_LBS FLOAT, PRICE_PER_LB FLOAT, CUPPING_SCORE FLOAT, HARVEST_DATE DATE,
         NOTE STRING, CATEGORY STRING, URGENCY FLOAT,
         FREIGHT_PREMIUM_USD_PER_LB FLOAT, DISCOUNT_PCT FLOAT, DELAY_WEEKS FLOAT,
         ENRICHMENT_STATUS STRING, LOADED_AT TIMESTAMP_TZ)""",
    """CREATE TABLE IF NOT EXISTS IMPORTS (
         RUN_ID STRING, FILE_NAME STRING, N_LOTS INTEGER, MEAN_PRICE_PER_LB FLOAT,
         STATUS STRING, LOADED_AT TIMESTAMP_TZ, DECIDED_AT TIMESTAMP_TZ, DECIDED_BY STRING)""",
    """CREATE OR REPLACE VIEW GOLD_IMPORT_STATS AS
         SELECT RUN_ID, FILE_NAME, N_LOTS, MEAN_PRICE_PER_LB, STATUS, LOADED_AT
         FROM IMPORTS WHERE STATUS IN ('published', 'released')""",
    """CREATE OR REPLACE VIEW GOLD_PURCHASES AS
         SELECT P.* FROM PURCHASES P JOIN GOLD_IMPORT_STATS G ON P.RUN_ID = G.RUN_ID""",
    """CREATE OR REPLACE VIEW GOLD_ORIGIN_SUMMARY AS
         SELECT ORIGIN, COUNT(*) AS LOTS, AVG(PRICE_PER_LB) AS AVG_PRICE_PER_LB,
                AVG(CUPPING_SCORE) AS AVG_CUPPING_SCORE, SUM(QUANTITY_LBS) AS TOTAL_LBS
         FROM GOLD_PURCHASES GROUP BY ORIGIN""",
]


def configured():
    return all(os.environ.get(k) for k in REQUIRED)


def connect():
    # SNOWFLAKE_PASSWORD may be a programmatic access token (Snowflake accepts it in place of a password)
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"], warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        database=os.environ["SNOWFLAKE_DATABASE"], schema=os.environ["SNOWFLAKE_SCHEMA"],
        paramstyle="qmark",
    )


def sync(db):
    """Replace this run's rows in Snowflake with the SQLite copy. Logged as a 'warehouse' step."""
    step = db.start_step("warehouse")
    if not configured():
        db.finish_step(step, "ok", "skipped: Snowflake not configured (SNOWFLAKE_* settings missing)")
        return "skipped"
    purchases = db.run_sql("warehouse", "SELECT * FROM purchases WHERE run_id = ?", (db.run_id,))
    imports = db.run_sql("warehouse", "SELECT * FROM imports WHERE run_id = ?", (db.run_id,))
    try:
        con = connect()
        try:
            cur = con.cursor()
            for ddl in SCHEMA:
                cur.execute(ddl)
            cur.execute("DELETE FROM PURCHASES WHERE RUN_ID = ?", (db.run_id,))
            cur.execute("DELETE FROM IMPORTS WHERE RUN_ID = ?", (db.run_id,))
            if purchases:
                cur.executemany(f"INSERT INTO PURCHASES VALUES ({', '.join('?' * 17)})",
                                [tuple(r) for r in purchases])
            if imports:
                cur.executemany(f"INSERT INTO IMPORTS VALUES ({', '.join('?' * 8)})", [tuple(r) for r in imports])
            con.commit()
        finally:
            con.close()
    except snowflake.connector.Error as err:
        db.finish_step(step, "failed", f"Snowflake error: {type(err).__name__}: {str(err).splitlines()[0]}")
        return "failed"
    db.finish_step(step, "ok", f"{len(purchases)} purchases and {len(imports)} import row copied to Snowflake",
                   rows_in=len(purchases), rows_out=len(purchases), rows_rejected=0)
    return "synced"
