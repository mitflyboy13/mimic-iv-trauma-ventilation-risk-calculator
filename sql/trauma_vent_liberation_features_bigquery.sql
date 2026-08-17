-- MIMIC-IV trauma ICU ventilator liberation cohort and predictors.
--
-- Edit the destination table before running:
--   `your_project.your_dataset.trauma_vent_liberation_features`
--
-- Default source datasets follow the public BigQuery naming convention used by
-- MIMIC-IV and MIT-LCP mimic-code derived concepts. If your release differs,
-- replace the dataset names below.

CREATE OR REPLACE TABLE `your_project.your_dataset.trauma_vent_liberation_features` AS
WITH diagnosis_flags AS (
    SELECT
        hadm_id
        , MAX(
            CASE
                WHEN icd_version = 9
                    AND SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 800 AND 959
                    AND SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) NOT BETWEEN 905 AND 909
                    AND SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) NOT BETWEEN 930 AND 939
                    THEN 1
                WHEN icd_version = 10
                    AND REGEXP_CONTAINS(icd_code, r'^[ST][0-9A-Z]')
                    THEN 1
                ELSE 0
            END
        ) AS trauma_flag
        , MAX(CASE
            WHEN icd_version = 9
                AND (
                    SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) IN (800, 801, 803, 804)
                    OR SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 850 AND 854
                ) THEN 1
            WHEN icd_version = 10
                AND (SUBSTR(icd_code, 1, 3) IN ('S02', 'S06')) THEN 1
            ELSE 0 END) AS tbi_flag
        , MAX(CASE
            WHEN icd_version = 9 AND SUBSTR(icd_code, 1, 3) IN ('805', '806') THEN 1
            WHEN icd_version = 10 AND SUBSTR(icd_code, 1, 3) IN ('S12', 'S13', 'S14', 'S22', 'S23', 'S24', 'S32', 'S33', 'S34') THEN 1
            ELSE 0 END) AS spine_flag
        , MAX(CASE
            WHEN icd_version = 9 AND SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 860 AND 862 THEN 1
            WHEN icd_version = 10 AND SUBSTR(icd_code, 1, 3) IN ('S20', 'S21', 'S22', 'S23', 'S24', 'S25', 'S26', 'S27', 'S28', 'S29') THEN 1
            ELSE 0 END) AS thoracic_trauma_flag
        , MAX(CASE
            WHEN icd_version = 9 AND SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 863 AND 869 THEN 1
            WHEN icd_version = 10 AND SUBSTR(icd_code, 1, 3) IN ('S30', 'S31', 'S32', 'S33', 'S34', 'S35', 'S36', 'S37', 'S38', 'S39') THEN 1
            ELSE 0 END) AS abdominal_pelvic_trauma_flag
        , MAX(CASE
            WHEN icd_version = 9
                AND (
                    SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 810 AND 829
                    OR SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 880 AND 897
                ) THEN 1
            WHEN icd_version = 10
                AND REGEXP_CONTAINS(icd_code, r'^S[4-9][0-9A-Z]') THEN 1
            ELSE 0 END) AS extremity_trauma_flag
        , MAX(CASE
            WHEN icd_version = 9 AND SAFE_CAST(SUBSTR(icd_code, 1, 3) AS INT64) BETWEEN 940 AND 949 THEN 1
            WHEN icd_version = 10 AND SUBSTR(icd_code, 1, 3) BETWEEN 'T20' AND 'T32' THEN 1
            ELSE 0 END) AS burn_flag
    FROM `physionet-data.mimiciv_3_1_hosp.diagnoses_icd`
    GROUP BY hadm_id
)
, trauma AS (
    SELECT
        hadm_id
        , trauma_flag
        , tbi_flag
        , spine_flag
        , thoracic_trauma_flag
        , abdominal_pelvic_trauma_flag
        , extremity_trauma_flag
        , burn_flag
        , (
            COALESCE(tbi_flag, 0)
            + COALESCE(spine_flag, 0)
            + COALESCE(thoracic_trauma_flag, 0)
            + COALESCE(abdominal_pelvic_trauma_flag, 0)
            + COALESCE(extremity_trauma_flag, 0)
            + COALESCE(burn_flag, 0)
        ) AS injury_body_region_count
        , CASE
            WHEN (
                COALESCE(tbi_flag, 0)
                + COALESCE(spine_flag, 0)
                + COALESCE(thoracic_trauma_flag, 0)
                + COALESCE(abdominal_pelvic_trauma_flag, 0)
                + COALESCE(extremity_trauma_flag, 0)
                + COALESCE(burn_flag, 0)
            ) >= 2 THEN 1 ELSE 0
        END AS polytrauma_proxy
    FROM diagnosis_flags
    WHERE trauma_flag = 1
)
, invasive_vent_events AS (
    SELECT
        stay_id
        , starttime
        , endtime
        , DATETIME_DIFF(endtime, starttime, HOUR) AS invasive_vent_duration_hours
        , ROW_NUMBER() OVER (PARTITION BY stay_id ORDER BY starttime) AS invasive_episode_number
    FROM `physionet-data.mimiciv_3_1_derived.ventilation`
    WHERE ventilation_status = 'InvasiveVent'
        AND endtime > starttime
)
, first_eligible_episode AS (
    SELECT *
    FROM invasive_vent_events
    WHERE invasive_episode_number = 1
        AND invasive_vent_duration_hours >= 24
)
, reinstitution AS (
    SELECT
        fe.stay_id
        , fe.starttime
        , fe.endtime
        , MIN(v.starttime) AS next_invasive_vent_starttime
    FROM first_eligible_episode fe
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.ventilation` v
        ON fe.stay_id = v.stay_id
            AND v.ventilation_status = 'InvasiveVent'
            AND v.starttime > fe.endtime
            AND v.starttime <= DATETIME_ADD(fe.endtime, INTERVAL 48 HOUR)
    GROUP BY fe.stay_id, fe.starttime, fe.endtime
)
, cohort AS (
    SELECT
        id.subject_id
        , id.hadm_id
        , id.stay_id
        , id.gender
        , id.race
        , adm.admission_type
        , id.icu_intime
        , id.icu_outtime
        , id.los_icu AS icu_los_days
        , id.los_hospital AS hospital_los_days
        , id.admission_age AS age
        , fe.starttime AS invasive_vent_starttime
        , fe.endtime AS liberation_time
        , fe.invasive_vent_duration_hours
        , CASE
            WHEN r.next_invasive_vent_starttime IS NULL
                AND (id.dod IS NULL OR id.dod > DATETIME_ADD(fe.endtime, INTERVAL 48 HOUR))
                THEN 1
            ELSE 0
        END AS liberation_success_48h
        , r.next_invasive_vent_starttime
        , id.dod
    FROM first_eligible_episode fe
    INNER JOIN `physionet-data.mimiciv_3_1_derived.icustay_detail` id
        ON fe.stay_id = id.stay_id
    INNER JOIN trauma tr
        ON id.hadm_id = tr.hadm_id
    INNER JOIN `physionet-data.mimiciv_3_1_hosp.admissions` adm
        ON id.hadm_id = adm.hadm_id
    LEFT JOIN reinstitution r
        ON fe.stay_id = r.stay_id
            AND fe.starttime = r.starttime
            AND fe.endtime = r.endtime
    WHERE id.admission_age >= 18
)
, vent_features AS (
    SELECT
        c.stay_id
        , ARRAY_AGG(vs.fio2 IGNORE NULLS ORDER BY vs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS fio2_last_6h
        , ARRAY_AGG(vs.peep IGNORE NULLS ORDER BY vs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS peep_last_6h
        , ARRAY_AGG(vs.tidal_volume_observed IGNORE NULLS ORDER BY vs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS tidal_volume_observed_last_6h
        , ARRAY_AGG(vs.respiratory_rate_set IGNORE NULLS ORDER BY vs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS respiratory_rate_set_last_6h
        , ARRAY_AGG(vs.respiratory_rate_total IGNORE NULLS ORDER BY vs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS respiratory_rate_total_last_6h
        , ARRAY_AGG(vs.plateau_pressure IGNORE NULLS ORDER BY vs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS plateau_pressure_last_6h
        , MAX(vs.respiratory_rate_total) AS respiratory_rate_total_max_24h
        , MAX(CASE
            WHEN REGEXP_CONTAINS(COALESCE(vs.ventilator_mode, vs.ventilator_mode_hamilton, ''), r'(?i)(SBT|PSV|CPAP/PSV|CPAP/PPS)')
                THEN 1 ELSE 0
        END) AS sbt_mode_proxy
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.ventilator_setting` vs
        ON c.stay_id = vs.stay_id
            AND vs.charttime > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
            AND vs.charttime <= c.liberation_time
    GROUP BY c.stay_id
)
, abg_features AS (
    SELECT
        c.stay_id
        , ARRAY_AGG(bg.ph IGNORE NULLS ORDER BY bg.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS ph_last_6h
        , ARRAY_AGG(bg.po2 IGNORE NULLS ORDER BY bg.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS pao2_last_6h
        , ARRAY_AGG(bg.pco2 IGNORE NULLS ORDER BY bg.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS paco2_last_6h
        , ARRAY_AGG(bg.bicarbonate IGNORE NULLS ORDER BY bg.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS bicarbonate_last_6h
        , ARRAY_AGG(bg.pao2fio2ratio IGNORE NULLS ORDER BY bg.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS pao2fio2_last_6h
        , MAX(bg.lactate) AS lactate_max_24h
        , MIN(bg.so2) AS so2_min_24h
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.bg` bg
        ON c.hadm_id = bg.hadm_id
            AND bg.charttime > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
            AND bg.charttime <= c.liberation_time
    GROUP BY c.stay_id
)
, neuro_features AS (
    SELECT
        c.stay_id
        , MIN(gcs.gcs) AS gcs_min_24h
        , ARRAY_AGG(gcs.gcs IGNORE NULLS ORDER BY gcs.charttime DESC LIMIT 1)[SAFE_OFFSET(0)] AS gcs_last_6h
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.gcs` gcs
        ON c.stay_id = gcs.stay_id
            AND gcs.charttime > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
            AND gcs.charttime <= c.liberation_time
    GROUP BY c.stay_id
)
, sofa_features AS (
    SELECT
        c.stay_id
        , ARRAY_AGG(sofa.sofa_24hours IGNORE NULLS ORDER BY sofa.endtime DESC LIMIT 1)[SAFE_OFFSET(0)] AS sofa_before_liberation
        , ARRAY_AGG(sofa.respiration_24hours IGNORE NULLS ORDER BY sofa.endtime DESC LIMIT 1)[SAFE_OFFSET(0)] AS sofa_respiration_before_liberation
        , ARRAY_AGG(sofa.cardiovascular_24hours IGNORE NULLS ORDER BY sofa.endtime DESC LIMIT 1)[SAFE_OFFSET(0)] AS sofa_cardiovascular_before_liberation
        , ARRAY_AGG(sofa.cns_24hours IGNORE NULLS ORDER BY sofa.endtime DESC LIMIT 1)[SAFE_OFFSET(0)] AS sofa_cns_before_liberation
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.sofa` sofa
        ON c.stay_id = sofa.stay_id
            AND sofa.endtime <= c.liberation_time
            AND sofa.endtime > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
    GROUP BY c.stay_id
)
, vaso_features AS (
    SELECT
        c.stay_id
        , CASE WHEN COUNT(ned.stay_id) > 0 THEN 1 ELSE 0 END AS vasopressor_any_24h
        , MAX(ned.norepinephrine_equivalent_dose) AS norepinephrine_equivalent_max_24h
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.norepinephrine_equivalent_dose` ned
        ON c.stay_id = ned.stay_id
            AND ned.starttime < c.liberation_time
            AND ned.endtime > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
    GROUP BY c.stay_id
)
, infection_features AS (
    SELECT
        c.stay_id
        , MAX(CASE WHEN soi.suspected_infection = 1 THEN 1 ELSE 0 END) AS suspected_infection_flag
        , MAX(CASE WHEN abx.stay_id IS NOT NULL THEN 1 ELSE 0 END) AS antibiotic_any_24h
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.suspicion_of_infection` soi
        ON c.stay_id = soi.stay_id
            AND soi.suspected_infection_time > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
            AND soi.suspected_infection_time <= c.liberation_time
    LEFT JOIN `physionet-data.mimiciv_3_1_derived.antibiotic` abx
        ON c.stay_id = abx.stay_id
            AND abx.starttime < c.liberation_time
            AND abx.stoptime > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
    GROUP BY c.stay_id
)
, sedation_features AS (
    SELECT
        c.stay_id
        , MAX(CASE
            WHEN REGEXP_CONTAINS(LOWER(pr.drug), r'(propofol|dexmedetomidine|midazolam|lorazepam|ketamine)')
                THEN 1 ELSE 0
        END) AS sedative_proxy_any_24h
        , MAX(CASE
            WHEN REGEXP_CONTAINS(LOWER(pr.drug), r'(fentanyl|hydromorphone|morphine|remifentanil|oxycodone)')
                THEN 1 ELSE 0
        END) AS opioid_proxy_any_24h
    FROM cohort c
    LEFT JOIN `physionet-data.mimiciv_3_1_hosp.prescriptions` pr
        ON c.hadm_id = pr.hadm_id
            AND pr.starttime < c.liberation_time
            AND COALESCE(pr.stoptime, pr.starttime) > DATETIME_SUB(c.liberation_time, INTERVAL 24 HOUR)
    GROUP BY c.stay_id
)
SELECT
    c.subject_id
    , c.hadm_id
    , c.stay_id
    , c.age
    , c.gender
    , c.race
    , c.admission_type
    , c.icu_intime
    , c.icu_outtime
    , c.invasive_vent_starttime
    , c.liberation_time
    , c.icu_los_days
    , c.hospital_los_days
    , c.invasive_vent_duration_hours
    , c.next_invasive_vent_starttime
    , c.liberation_success_48h
    , tr.tbi_flag
    , tr.spine_flag
    , tr.thoracic_trauma_flag
    , tr.abdominal_pelvic_trauma_flag
    , tr.extremity_trauma_flag
    , tr.burn_flag
    , tr.injury_body_region_count
    , tr.polytrauma_proxy
    , oasis.oasis
    , charlson.charlson_comorbidity_index
    , nf.gcs_min_24h
    , nf.gcs_last_6h
    , sf.sofa_before_liberation
    , sf.sofa_respiration_before_liberation
    , sf.sofa_cardiovascular_before_liberation
    , sf.sofa_cns_before_liberation
    , vf.vasopressor_any_24h
    , vf.norepinephrine_equivalent_max_24h
    , inf.suspected_infection_flag
    , inf.antibiotic_any_24h
    , sed.sedative_proxy_any_24h
    , sed.opioid_proxy_any_24h
    , abg.ph_last_6h
    , abg.pao2_last_6h
    , abg.paco2_last_6h
    , abg.bicarbonate_last_6h
    , abg.pao2fio2_last_6h
    , abg.lactate_max_24h
    , abg.so2_min_24h
    , vent.fio2_last_6h
    , vent.peep_last_6h
    , CAST(NULL AS FLOAT64) AS pressure_support_last_6h
    , vent.tidal_volume_observed_last_6h
    , vent.respiratory_rate_set_last_6h
    , vent.respiratory_rate_total_last_6h
    , vent.respiratory_rate_total_max_24h
    , vent.plateau_pressure_last_6h
    , SAFE_DIVIDE(vent.plateau_pressure_last_6h - vent.peep_last_6h, NULLIF(vent.tidal_volume_observed_last_6h / 1000.0, 0)) AS compliance_proxy_last_6h
    , vent.plateau_pressure_last_6h - vent.peep_last_6h AS driving_pressure_proxy_last_6h
    , SAFE_DIVIDE(vent.respiratory_rate_total_last_6h, NULLIF(vent.tidal_volume_observed_last_6h / 1000.0, 0)) AS rsbi_proxy_last_6h
    , vent.sbt_mode_proxy
    , CASE
        WHEN vent.peep_last_6h <= 8
            AND vent.fio2_last_6h <= 50
            THEN 1
        ELSE 0
    END AS low_support_proxy
FROM cohort c
INNER JOIN trauma tr
    ON c.hadm_id = tr.hadm_id
LEFT JOIN vent_features vent
    ON c.stay_id = vent.stay_id
LEFT JOIN abg_features abg
    ON c.stay_id = abg.stay_id
LEFT JOIN neuro_features nf
    ON c.stay_id = nf.stay_id
LEFT JOIN sofa_features sf
    ON c.stay_id = sf.stay_id
LEFT JOIN vaso_features vf
    ON c.stay_id = vf.stay_id
LEFT JOIN infection_features inf
    ON c.stay_id = inf.stay_id
LEFT JOIN sedation_features sed
    ON c.stay_id = sed.stay_id
LEFT JOIN `physionet-data.mimiciv_3_1_derived.oasis` oasis
    ON c.stay_id = oasis.stay_id
LEFT JOIN `physionet-data.mimiciv_3_1_derived.charlson` charlson
    ON c.hadm_id = charlson.hadm_id
;
