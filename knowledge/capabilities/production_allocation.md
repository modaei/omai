# Capability: Production Allocation Report

Use this capability when a user asks how to know how much a specific well contributed to oil, gas, or water production.

Ometrics provides a production allocation report. The report allocates measured production back to wells for a selected date range. It is the appropriate place to answer questions like "how much did Well 6243 contribute to oil production", "which wells contributed most", or "show the allocated production by well".

The allocation logic can account for well status and shutdown time. Short hourly shutdowns and long shutdown ranges can affect the up-time ratio used by allocation calculations.

When answering workflow questions, only point the user to the Production Allocation report. Keep the answer to maximum two sentences unless the user asks for details. Do not include sections such as "what the report gives", "what to check before running", or pre-checklists. Do not invent UI steps, site selection, grouping options, metric selectors, optional filters, exports, or pre-checklists. If the user asks for actual values, use the report tool rather than guessing.

Related reports and code areas: production_allocation, oil_production, gas_production, water_production.
