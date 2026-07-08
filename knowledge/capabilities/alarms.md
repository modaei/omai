# Capability: Alarms

Use this capability when a user asks how Ometrics detects abnormal monitored values, high or low conditions, boolean alarms, timeout alarms, or alarm events.

Ometrics alarm processing evaluates monitored data points against configured thresholds. Numeric data points can have high alarm, high warning, low warning, and low alarm thresholds. Boolean data points can trigger flag alarms. Timeout logic can detect stale or missing monitored data.

Alarms are useful for identifying abnormal field conditions that need attention, such as pressure outside limits, equipment states, missing telemetry, or other monitored conditions.

When answering workflow questions about how to know whether a device is in an error state, point the user to the Alarms page. Do not tell the user to use well timelines for this workflow unless they ask for a well investigation or historical timeline.

This assistant is read-only. It can explain alarm capabilities and include alarm records in a well timeline, but it should not claim it can acknowledge alarms or change alarm configuration unless a future tool is added.

Related code area: omalarm analyzer and alarm data point processing. Related assistant tool: get_well_timeline.
