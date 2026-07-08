# Capability: Well Shutdowns

Use this capability when a user asks how to record that a well is down, shut in, offline, not producing, or back online.

Ometrics tracks well downtime with well shutdown records. Short downtime can be entered as hourly shutdowns, where the user records the well, date, downtime code, and number of hours. Extended downtime can be entered as long shutdowns, where the user records the well, downtime code, start timestamp, and optionally an end timestamp. A long shutdown with no end timestamp means the well is still shut down.

Downtime codes describe why the well is down. They are used later by reports and timeline views to explain production loss and operating history.

When answering workflow questions, point the user to the Well Shutdowns feature. Use the quoted guidance when the user asks how to register or create a well shutdown: "Use the Create Well Shutdown form, accessible from the dashboard or the Create Well Shutdown button in the well shutdowns page." If the user asks which shutdown type to use, explain that hourly shutdowns are appropriate for a known number of downtime hours on a day, while long shutdowns are appropriate when the well is shut in for a continuous period or is still down.

Related tools in this assistant: get_well_shutdowns, get_current_long_shutdowns, list_downtime_codes, get_well_timeline.
