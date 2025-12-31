import os 
import json 
from dotenv import load_dotenv
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions, StandardOptions
from apache_beam.transforms.window import FixedWindows


#Load Env Variables 
load_dotenv()
PROJECT_ID = os.getenv("GCP_PROJECT")
PUBSUB_SUBSCRIPTION = os.getenv("PUBSUB_SUBSCRIPTION")
BRONZE_PATH = os.getenv("BRONZE_PATH")
SILVER_PATH = os.getenv("SILVER_PATH")
BIGQUERY_TABLE = os.getenv("BIGQUERY_TABLE")
TEMP_LOCATION = os.getenv("TEMP_LOCATION")
STAGING_LOCATION = os.getenv("STAGING_LOCATION")
REGION = os.getenv("REGION")


#Beam Pipeline Options

Pipeline_Options = PipelineOptions(
    streaming=True,
    save_main_session=True,
    runner="DataflowRunner",
    project=PROJECT_ID,
    region=REGION,
    temp_location=TEMP_LOCATION,
    staging_location=STAGING_LOCATION
    )

#parse 
def parson_json(message):
    try:
        data = json.loads(message)
        return data
    except json.JSONDecodeError:
        return None

def is_valid_record(record):
    try:
        if record is None:
            return False
        p_id = record.get("patient_id")
        hr = record.get("heart_rate")
        spo2 = record.get("spo2")
        temp = record.get("temperature")
        bp_sys = record.get("bp_systolic")
        bp_dia = record.get("bp_diastolic")
        # Ensure all required fields are present
        if p_id is None or hr is None or spo2 is None or temp is None or bp_sys is None or bp_dia is None:
            return False

        # Validate ranges
        if not (0 < spo2 <= 100):
            return False
        if not (0 < hr < 200):
            return False
        if not (30 <= temp <= 45):
            return False

        return True
    except Exception:
        return False
    
def enrich_record(record):
    record["risk_score"] = (
        (record["heart_rate"]/200)*0.4 +
        (record["temperature"]/40)*0.3 +
        (1 - record["spo2"]/100)*0.3
    )
    if record["risk_score"] < 0.3:
        record["risk_level"] = "Low"
    elif record["risk_score"] < 0.6:
        record["risk_level"] = "Moderate"
    else:
        record["risk_level"] = "High"
    return record

#pipeline 
with beam.Pipeline(options=Pipeline_Options) as p:
    #Bronze Layer 
    bronze_data = (
        p
        | "Read from PubSub" >> beam.io.ReadFromPubSub(subscription=PUBSUB_SUBSCRIPTION)
        | "decode to string " >> beam.Map(lambda x: x.decode("utf-8"))
        | "Window Bronze data" >> beam.WindowInto(FixedWindows(60))
    )

    #write raw to Bronze GCS
    bronze_data | "Write to Bronze GCS" >> beam.io.WriteToText(
        BRONZE_PATH + "raw_data",
        file_name_suffix=".json"

        )

    #Silver Layer 
    silver_data = (
        bronze_data
        | "Parse JSON" >> beam.Map(parson_json)
        | "Filter Valid Records" >> beam.Filter(is_valid_record)
        | "Enrich Records" >> beam.Map(enrich_record)
        | "Window Silver Data" >> beam.WindowInto(FixedWindows(60))
    )
    #write to Silver GCS
    silver_data | "Write to Silver GCS" >> beam.io.WriteToText(
        SILVER_PATH + "cleaned_data",
        file_name_suffix=".json"
    )

 # ------------------- Gold Layer -------------------

    def extract_for_aggregation(record):
        return (record["patient_id"], record)

    def aggregate_records(key_values):
        patient_id, records_iter = key_values
        records = list(records_iter)
        count = len(records)

        avg_heart_rate = sum(r["heart_rate"] for r in records) / count
        avg_spo2 = sum(r["spo2"] for r in records) / count
        avg_temp = sum(r["temperature"] for r in records) / count

        risk_levels = {r["risk_level"] for r in records}
        if "High" in risk_levels:
            max_risk = "High"
        elif "Moderate" in risk_levels:
            max_risk = "Moderate"
        else:
            max_risk = "Low"

        return {
            "patient_id": patient_id,
            "count": count,
            "avg_heart_rate": avg_heart_rate,
            "avg_spo2": avg_spo2,
            "avg_temp": avg_temp,
            "max_risk_level": max_risk,
        }


    gold_data = (
        silver_data
        | "Extract for Aggregation" >> beam.Map(extract_for_aggregation)
        | "Group by Patient ID" >> beam.GroupByKey()
        | "Aggregate Records" >> beam.Map(aggregate_records)
    )

    #write to BigQuery
    gold_data | "Write to BigQuery" >> beam.io.WriteToBigQuery(
        table=BIGQUERY_TABLE,
        method=beam.io.WriteToBigQuery.Method.STREAMING_INSERTS,
        write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND,
        create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
    )




    


 
