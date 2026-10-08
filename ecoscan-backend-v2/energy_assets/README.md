# Energy assets

## Consumption anomaly detection

Apply the V2 migrations before running the analysis:

```powershell
python manage.py migrate
```

Run `python manage.py analyser_anomalies_telemetrie` once per day after daily
energy readings are available. By default, it analyzes the previous local
calendar day for each site. Use `--date YYYY-MM-DD` to select a local day and
`--site <UUID>` to analyze a single site.

For a site to be analyzed, each active `ENERGY` sensor on a monitored equipment
must have at least four readings on the analyzed day and on each of the four
previous matching weekdays. The daily usage is the difference between the
first and last cumulative reading for that day. If any day or sensor lacks
sufficient readings, the analysis reports insufficient data and creates no
anomaly.

The observed daily usage is compared with the simple average of the four
reference days, following the existing analysis baseline quality requirements.
Variations below 10% are considered normal; 10%, 20%, and 40% mark the
surveillance, alert, and priority-investigation thresholds used by the existing
anomaly service. Detected anomalies are available at
`/api/energy-assets/sites/{site_id}/anomalies/`.
