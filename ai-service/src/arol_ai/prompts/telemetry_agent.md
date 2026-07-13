# Telemetry-Agent Prompt

Analyze the latest machine telemetry.

Focus on:

- Active alarms.
- RPM.
- Torque.
- Temperature.
- Missing or stale sensor data.
- Whether the telemetry supports a warning, critical, healthy, or offline state.

Do not recommend bypassing safety protections. Escalate persistent alarms to qualified maintenance personnel.

Record telemetry access as a tool call, including whether the snapshot was found or missing.
