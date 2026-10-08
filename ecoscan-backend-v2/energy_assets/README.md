# Energy assets

## Capteur simulator

Run the simulator as a separate process alongside Django:

```powershell
python manage.py simuler_capteurs
```

The command checks active `SIMULATED` sensors once per second and emits a
measurement when each sensor's `frequence_secondes` interval has elapsed. Use
`python manage.py simuler_capteurs --once` to run one check, or
`--interval 2` to change the polling interval. The polling interval does not
change each sensor's configured measurement frequency.

`POWER` sensors report the equipment's nominal power while it is on and its
standby power while it is off. The simulator follows the desired state first,
then the reported state; if both are unknown, it starts the simulated equipment
as on. `ENERGY` sensors integrate the simulated power into a cumulative kWh
reading. Unsupported sensor types and real sensors are left untouched.

The generated measurements use the regular telemetry synchronization service,
so they update equipment state, cumulative energy, and sensor communication
timestamps in the same way as ingested readings. The simulator does not send
commands to physical equipment.
