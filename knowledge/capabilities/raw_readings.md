# Capability: Raw Field Readings

Use this capability when a user asks where or how to enter or inspect daily field readings.

Ometrics stores daily readings for LACT units, flares, tanks, water plants, flow meters, well tests, well fluid levels, well injections, run tickets, water draws, treaters, knock-outs, and pumps. These readings are the operational source data used by reports and troubleshooting workflows.

For one-day questions, the assistant can retrieve readings for a selected reading type and date. For date-range questions, the assistant can search readings across a date range and apply numeric filters such as oil greater than a value, pressure above a threshold, or LACT reading over a value.

When answering workflow questions about registering or entering a reading, answer only with the create/update action for that reading type. For example, for a LACT value, say "Create a new LACT reading." Do not include "what to record", "why it matters", assistant tool lists, or follow-up offers unless the user asks for details.

Related tools in this assistant: get_readings_for_date, search_readings, compare_readings_between_dates, find_missing_readings.
