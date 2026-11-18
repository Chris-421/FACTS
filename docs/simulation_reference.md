# Factory inputs for RFQ assessment

FACTS uses the same factory and starting snapshot for the RFQ forecast and the paired checks of existing orders and operational risk. [Run the RFQ workflow](rfq_decision.md)

## Input tables

Pass six pandas DataFrames to `FactoryData`, or use `load_workbook(path, start=...)` with six Excel sheets of the same names. All durations are in minutes and timestamps use factory-local time without a timezone. IDs uniquely identify rows within their own table. In Excel, enter nested lists and mappings as Python literals, such as `['cut', 'finish']` or `{1: {'paint': 0.5}}`.

| DataFrame | Required columns | Nested fields and optional columns |
|---|---|---|
| `FACTS` | `factory_ID`, `factory_name`, `industry`, `n_employees`, `shift_schedule`, `shift_exceptions`, `scheduling_policy`, `priority_rules` | One row. `shift_schedule` contains records with `shift_number`, `Shift_ID`, `weekday` (Monday = 0), `start`, and `end`. `shift_exceptions` maps a date to replacement shift records for that date, or `[]` for closure. `scheduling_policy` is `FIFO` or `EDD`. `{}` uses the confirmed priorities. |
| `Parts` | `ID_pt`, `routing`, `BOM` | `routing` is a list of process IDs. `BOM` maps zero-based routing steps to material quantities per part unit. Omitted steps use no extra material. A list with one material mapping per step is also accepted. |
| `Processes` | `ID_proc`, `tp`, `tsu`, `WS_groups` | `tp` is minutes per unit or a map from part ID to minutes per unit. `tsu` is blank, a fixed number, or a map from `(previous_part, current_part)` to minutes. `WS_groups` lists compatible groups. |
| `Workstations` | `ID_WS`, `WS_group`, `availability`, `t_down` | `availability` is `available`, `processing`, or `unavailable`. Boolean values map to `available` and `unavailable`. For initially unavailable stations, `t_down` is the remaining time before recovery. Optional columns include `previous_part` and `unavailability_reason`. |
| `Orders` | `ID_O`, `lines`, `RD`, `DD`, `status` | `lines` is a list of `(part_id, quantity)` pairs. Each line is a job, and the last job to finish determines the order completion time. `status` is `completed` or `outstanding`. RFQs use the same schema with distinct IDs. |
| `Materials` | `ID_mat`, `family`, `suppliers`, `q`, `DLT`, `OTP`, `last_order` | `suppliers` is a list. `q` is purchase quantity, `DLT` is lead time, and `OTP` is time from one placement to the next. `DLT` can map supplier IDs to their lead times. A material with several suppliers needs an explicit selector when it places an order. |

Regular shift numbers start at zero and repeat weekly. An end time earlier than the start time carries into the next day. Date exceptions replace occurrences that start on that date. The loader rejects overlapping occurrences, including overlaps across midnight. A job can start only if the full setup and processing time fits within the active shift.

### Starting snapshot

Construct `Snapshot(start, wip=..., inventory=..., pending_deliveries=...)` or use the optional Excel sheets below.

| Sheet | Fields |
|---|---|
| `WIP` | `ID_O`, one-based `line`, `state`, zero-based `step`. Supply `ID_WS` and `remaining_minutes` for processing jobs, `ready_at` for the current queue arrival of waiting jobs, and `completed_at` for finished lines in outstanding orders. An outstanding order released before `start` needs one WIP row per line. |
| `Inventory` | `ID_mat`, `cq` for each material, and `next_order_time` when another purchase is scheduled. `InventoryRules.initialize_order` can supply an explicit first-order policy using the material record and snapshot timestamp. |
| `PendingDeliveries` | `ID_PO`, `ID_mat`, `q`, `arrival`, and optional `supplier`. These carry their expected arrival into each scenario. |

Stock is consumed at the start of the step that needs it. Shortages make that step ineligible until sufficient stock arrives. The quantity and arrival of a new purchase are determined at placement, and stock changes at delivery. A blank `next_order_time` with no initialization policy means there is no scheduled next placement.

FIFO ranks by arrival at the current process queue. EDD ranks by order due date. Ties use the shortest eligible operation including setup, then queue arrival, then the input order of jobs. Eligible stations rank by shortest total operation duration, with workstation input order deciding a tie. A lower-priority job can run when a higher-priority job does not fit. `priority_rules` accepts explicitly configured `fifo_basis`, `backfill`, `job_tiebreak`, or `station_selection` overrides. The last two accept `'id'` or a rank function in Python.

## Efficiency in the RFQ forecast

FACTS samples one efficiency per trajectory from the planner's estimate and bounds or explicit weights. At the end of a productive operation, including setup, it draws a Poisson count and sums that many independent capped Pareto downtimes. Each draw lasts 1–480 working minutes. The combined outage can exceed 480 minutes and its clock pauses during factory closures.

The central 80/20 case supplies the production completion forecast and commitment, while 70/30 and 90/10 measure sensitivity. FACTS calibrates the Poisson rate using the mean of each capped distribution. The quotation runs use nominal supplier lead times. [Forecast, commitment, and sensitivity](rfq_decision.md#commitment-and-pareto-sensitivity)

## Machine and supplier risk inputs

The operational-risk analysis ranks machine and supplier effects from matched runs. It uses the forecast horizon as the observation cutoff unless you supply a cutoff through `RiskSettings`.

- **Machines.** Provide `tbf`, `ttr`, and `initial_tbf_remaining` in `Workstations`, or supply a `MachineReliability(tbf, ttr, initial_tbf_remaining)` for each relevant workstation in `RiskSettings`. Supply fixed values or functions that take a NumPy random generator. The failure clock runs during productive operations, including setup. Failures take effect after the running operation finishes. Repairs then advance on the calendar.
- **Suppliers.** Use `Parameter(nominal, sampler)` for variable lead time in the `Materials` table. Use `InventoryRules(sample_otp=True)` to sample a configured purchase-order interval in the operational-risk analysis. For materials with multiple suppliers, supply the selection policy through `InventoryRules`.

[Read the machine and supplier risk findings](rfq_decision.md#machine-and-supplier-risks)
