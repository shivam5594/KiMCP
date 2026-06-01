# Tool Catalog

Map from KiCAD features → MCP tools. Goal: every menu item, keyboard shortcut, `kicad-cli` subcommand, and file-format object is reachable through the MCP.

## Naming conventions

- `list_*` — returns many items, paged, filterable.
- `get_*` — returns one item by id or selector.
- `add_*` / `create_*` — creates new state. `add` = into existing container; `create` = new top-level artifact.
- `edit_*` — partial update. Supports `patch`-style.
- `delete_*` / `remove_*` — `delete` = removes from document; `remove` = unregisters (e.g., a library).
- `move_*` / `rotate_*` / `flip_*` / `mirror_*` — geometric ops.
- `run_*` — starts a process (DRC, ERC, simulate, autoroute).
- `export_*` / `import_*` — file I/O crossing a format boundary.
- `find_*` — search / query across artifacts.
- `suggest_*` — domain-knowledge engine outputs (decoupling, return path, alternates).
- `validate_*` / `check_*` — rule evaluation without mutation.

## Categories

### Project
`create_project`, `open_project`, `close_project`, `project_info`, `list_boards`, `duplicate_project`, `archive_project`, `set_project_settings`, `get_project_settings`, `set_drawing_sheet`, `get_drawing_sheet`, `set_revision`, `snapshot_project`, `restore_snapshot`.

### Schematic (eeschema)
`list_sheets`, `get_sheet`, `add_sheet`, `delete_sheet`, `rename_sheet`, `push_into_sheet`, `pop_to_root`.

Component ops: `list_components`, `get_component`, `add_component`, `edit_component`, `delete_component`, `duplicate_component`, `move_component`, `rotate_component`, `mirror_component`, `set_component_fields`.

Connectivity: `list_nets`, `get_net`, `rename_net`, `merge_nets`, `split_net`.

Drawing: `add_wire`, `delete_wire`, `add_junction`, `delete_junction`, `add_label`, `add_global_label`, `add_hierarchical_label`, `add_power_symbol`, `add_no_connect`, `add_bus`, `rip_bus_entry`, `add_text_annotation`, `add_graphic_line`, `add_graphic_shape`.

Validation & annotation: `annotate`, `reannotate`, `reset_annotations`, `run_erc`, `get_erc_violations`, `export_erc_report`.

Outputs: `export_netlist` (multiple formats: KiCadNet, OrcadPCB2, Spice, CadStar, Allegro, …), `export_bom` (XML, CSV, grouped, per-field), `export_schematic_pdf`, `export_schematic_svg`.

Cross-tool: `cross_probe_to_pcb`, `backannotate_from_pcb`, `update_pcb_from_schematic`, `update_schematic_from_pcb`.

Simulation: `simulate_dc`, `simulate_ac`, `simulate_transient`, `simulate_noise`, `simulate_distortion`, `get_simulation_plot`, `set_sim_probe`.

### Symbol library
`list_symbol_libraries`, `register_symbol_library`, `unregister_symbol_library`, `list_symbols_in_library`, `get_symbol`, `create_symbol`, `edit_symbol`, `delete_symbol`, `duplicate_symbol`.

Symbol internals: `add_pin`, `edit_pin`, `delete_pin`, `set_pin_alternates`, `add_unit`, `delete_unit`, `set_demorgan`, `add_symbol_field`, `edit_symbol_field`, `delete_symbol_field`.

Linkage: `assign_footprint`, `assign_3d_model`, `set_datasheet_url`, `set_mpn`, `set_distributor_part`.

### PCB (pcbnew)

Board frame: `get_board_info`, `get_board_extents`, `set_board_size`, `add_board_outline`, `edit_board_outline`, `add_mounting_hole`, `add_fiducial`.

Stackup & layers: `get_stackup`, `set_stackup`, `list_layers`, `add_layer`, `remove_layer`, `set_active_layer`, `set_layer_constraints`, `set_layer_visibility`.

Placement: `list_footprints_on_board`, `get_footprint_on_board`, `add_footprint_to_board`, `delete_footprint_from_board`, `move_footprint`, `rotate_footprint`, `flip_footprint`, `align_footprints`, `distribute_footprints`, `array_place`, `group_footprints`, `ungroup_footprints`.

Pads: `list_pads`, `get_pad`, `edit_pad`, `get_pad_position`.

Connectivity: `list_nets_on_board`, `get_net_on_board`, `rename_net_on_board`, `get_net_at_point`, `get_net_connections`, `find_orphaned_wires`, `find_overlapping_elements`.

Net classes & rules: `list_netclasses`, `create_netclass`, `edit_netclass`, `delete_netclass`, `assign_net_to_class`, `set_design_rules`, `get_design_rules`, `add_custom_rule`.

Routing: `route_trace`, `delete_trace`, `modify_trace`, `route_pad_to_pad`, `route_differential_pair`, `length_tune`, `meander`, `copy_routing_pattern`, `add_via`, `delete_via`, `add_blind_buried_via`, `add_micro_via`.

Zones & pours: `add_zone`, `edit_zone`, `delete_zone`, `refill_zones`, `add_copper_pour`, `add_thermal_relief`, `add_keepout_zone`.

Text / graphics: `add_board_text`, `edit_board_text`, `delete_board_text`, `add_dimension`, `add_graphic_line`, `add_graphic_shape`, `import_svg_logo`.

Validation: `run_drc`, `get_drc_violations`, `export_drc_report`, `check_clearance`, `check_courtyard_overlap`.

External routing: `export_dsn`, `import_ses`, `autoroute_freerouting`.

Outputs: `export_gerber`, `export_drill`, `export_position_file`, `export_pdf`, `export_svg`, `export_3d_step`, `export_vrml`, `export_idf`, `render_3d_image`.

Sync: `sync_schematic_to_board`, `get_sync_diff`.

### Footprint library
`list_footprint_libraries`, `register_footprint_library`, `unregister_footprint_library`, `list_footprints_in_library`, `get_footprint`, `create_footprint`, `edit_footprint`, `delete_footprint`, `duplicate_footprint`.

Footprint internals: `add_pad_to_footprint`, `edit_pad_in_footprint`, `delete_pad_from_footprint`, `set_courtyard`, `set_fabrication_layer`, `assign_3d_model_to_footprint`, `add_footprint_text`.

### Drawing sheet (page frame)
`load_drawing_sheet`, `save_drawing_sheet`, `create_drawing_sheet`, `edit_title_block`, `set_revision`, `set_sheet_size`.

### Calculators (from KiCad PCB Calculator)
`trace_width_for_current`, `via_current_capacity`, `impedance_microstrip`, `impedance_stripline`, `impedance_differential`, `track_attenuation`, `transmission_line_reflections`, `color_code`, `rf_attenuator`, `regulator_feedback`.

### Simulation (ngspice)
`create_sim_schema`, `add_sim_probe`, `run_sim`, `get_sim_results`, `plot_sim`, `export_sim_csv`.

### Domain-knowledge tools (cross-cutting — populated from sibling skills)
`validate_design` — runs all applicable rules from domain skills.
`suggest_decoupling` — from `power-integrity`.
`suggest_bulk_capacitance` — from `power-integrity`.
`suggest_return_path` — from `signal-integrity`.
`suggest_termination` — from `signal-integrity`.
`check_dfm` — from `dfm`.
`check_fab_compatibility` — from `dfm`, against a configured fab-capability profile.
`find_datasheet` — from `datasheet-search`.
`extract_datasheet_facts` — from `datasheet-search`.
`find_errata` — from `errata-search`.
`compare_vendors` — from `vendor-search`.
`check_lifecycle` — from `vendor-search`.
`find_footprint` — from `3d-models-and-footprints-search`.
`find_3d_model` — from `3d-models-and-footprints-search`.
`validate_footprint_vs_datasheet` — from `3d-models-and-footprints-search` + `datasheet-search`.

## Coverage rule

A file `tests/coverage_matrix.md` in the implementation repo lists every KiCAD menu item, every keyboard shortcut, and every `kicad-cli` subcommand → the MCP tool(s) that cover it, or an explicit `NOT APPLICABLE` row with reason. This file must be green before any release. Adding a new tool without updating the matrix is a CI failure.

## Versioning

Each tool has a `version` field (semver). Breaking schema changes bump major. Deprecated tools stay for one minor version with a `deprecated_in`/`remove_in` field returned in metadata.
