# Explicit benchmark execution profiles

`system-controller.json` is an execution profile, separate from the frozen input
engineering benchmark. It is not a user-provided mechanical contract.

The initial benchmark inferred fixed status for every jumper/connector. Three
fixed pairs have incompatible conservative courtyard separation. This profile
allows the internal RS201 and COM_SEL201/202/203 configuration jumpers to move.
All other inferred fixed references remain fixed. Their new locations are subject
to the same board, gap, proximity and native DRC checks as every other movable part.

No electrical connection, pin type, footprint, manufacturing minimum or error
severity is changed. The original constraints remain in the frozen benchmark as
an infeasibility regression. Both profiles and the exact chosen revision are
recorded by the engineering workflow. A customer board must use the customer's
actual mechanical and electrical requirements, not this benchmark profile.

The execution profile now selects `placement.algorithm = legalize`: conflict
groups are repaired with a minimum-displacement objective before the independent
geometry audit. It intentionally preserves the original component arrangement,
instead of optimizing HPWL by moving already legal parts. The `auto`, `slsqp`,
and `block_coordinate` modes remain available for other placement objectives.

The shipped WSL toolchain explicitly enables `controlled_neckdown`. This allows
the router to narrow a preferred-width trace at a dense pad escape. Preferred
netclass widths are not silently rewritten. Native import measures actual segment
widths, and final DRC plus declared per-net minima still control release. The
option is off by default in the schema, and does not authorize a change to any
manufacturing minimum or a customer-specified net width.

The WSL profile also explicitly enables `fanout` for pinned Freerouting 1.9.0.
It derives a complete routing-settings sidecar from native KiCad DSN and enables
the router's existing fanout phase; it is not a new custom routing algorithm.
Input DSN and physical rules are unchanged. Settings, file hashes and native
logs are retained. The pinned 2.4.1 backend has its own independently verified
fanout profile; 2.0.1 rejects this option. The real
small-board A/B and native phase readback are in
`docs/validation/native-v2/fanout-v1/`; a successful small sample does not prove
the 160-component benchmark or every 2/4/6/8-layer geometry will route fully.

`toolchain.legacy-baseline.json` disables fanout while keeping the same 1.9.0
engine and manufacturing inputs. It is the explicit control profile for testing
the corrected via-diameter compiler. `toolchain.unified.json` selects the isolated
Java 25 / 2.4.1 engine; its results must be reported separately, including timeouts.
