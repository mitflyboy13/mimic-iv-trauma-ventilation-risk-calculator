"""Feature schema conventions for the trauma ventilator model."""

TARGET_COLUMN = "liberation_success_48h"

IDENTIFIER_COLUMNS = {
    "subject_id",
    "hadm_id",
    "stay_id",
}

TIME_COLUMNS = {
    "icu_intime",
    "icu_outtime",
    "invasive_vent_starttime",
    "liberation_time",
    "next_invasive_vent_starttime",
    "dod",
}

DEFAULT_EXCLUDE_COLUMNS = IDENTIFIER_COLUMNS | TIME_COLUMNS | {TARGET_COLUMN}

REQUIRED_FEATURES = [
    "age",
    "gender",
    "race",
    "admission_type",
    "icu_los_days",
    "invasive_vent_duration_hours",
    "tbi_flag",
    "spine_flag",
    "thoracic_trauma_flag",
    "abdominal_pelvic_trauma_flag",
    "extremity_trauma_flag",
    "burn_flag",
    "injury_body_region_count",
    "polytrauma_proxy",
    "gcs_min_24h",
    "sofa_before_liberation",
    "pao2fio2_last_6h",
    "fio2_last_6h",
    "peep_last_6h",
    "rsbi_proxy_last_6h",
    "sbt_mode_proxy",
    "low_support_proxy",
]

