#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "json.hpp"

using json = nlohmann::json;

/*
    Hydraulic press digital-twin solver
    -----------------------------------
    This executable has two responsibilities:

    1. Single network solve
       - Reads network.json.
       - Solves one hydraulic operating mode.
       - Prints node pressure, cell flow, velocity, loss, pump, and valve results.

    2. CSV-based realtime wrapper
       - Reads rows from an input CSV.
       - Applies each row to a temporary network.runtime.json.
       - Runs the same solver for each new row.
       - Appends selected results to an output CSV.

    The physical model is intentionally simple and readable:
    - 1D node-cell network.
    - Incompressible hydraulic oil.
    - Darcy-Weisbach pipe loss.
    - Kv-based valve pressure loss when kv_max_m3_h is configured.
    - Variable-speed pump head curve.

    Important limitation:
    This is a quasi-steady solver. It does not simulate full fluid compressibility,
    cylinder chamber volume dynamics, spool transient motion, or detailed leakage.
*/

namespace {
constexpr double kAtmosphericPressureBar = 1.01325;
constexpr double kPi = 3.14159265358979323846;

double bar_to_pa(double bar) {
    return bar * 100000.0;
}
}

struct Fluid {
    // Fluid properties used by every pipe/valve loss calculation.
    // Viscosity is corrected from the configured reference value by temperature.
    std::string name;
    double density_kg_m3 = 860.0;
    double viscosity_pa_s = 0.0396;
    double reference_temperature_c = 40.0;
    double reference_viscosity_pa_s = 0.0396;
    double temperature_c = 40.0;
    double gravity_m_s2 = 9.80665;
};

struct Node {
    // A node is a hydraulic connection point: tank port, pump port, valve port,
    // cylinder chamber, or relief return point.
    std::string id;
    std::string name;
    std::string type;

    // Elevation is used to convert between pressure and hydraulic head.
    double elevation_m = 0.0;

    // pressure_bar_g is gauge pressure. 0 bar(g) means atmospheric reference,
    // not absolute vacuum. pressure_bar_abs is derived for suction/NPSH checks.
    double pressure_bar_g = 0.0;
    double pressure_bar_abs = kAtmosphericPressureBar;
    double head_m = 0.0;
    bool has_pressure_boundary = false;
};

struct Cell {
    // A cell is a hydraulic component between two nodes.
    // type is normally "pipe", "pump", or "valve".
    std::string id;
    std::string type;
    std::string line_type;
    std::string from_node;
    std::string to_node;

    // Pipe geometry and local loss data. For valves, diameter is still used
    // to report velocity; pressure loss is usually calculated from Kv.
    double length_m = 0.0;
    double diameter_m = 0.0;
    double roughness_m = 0.0;
    double local_loss_k = 0.0;
    int bend_count = 0;
    bool allow_reverse_flow = false;

    // Pump fields. The implemented curve is:
    // H = (shutoff_head - coeff * equivalent_rated_flow^2) * speed_ratio^2
    bool is_running = true;
    int rpm = 0;
    int rated_rpm = 0;
    double pump_head_shutoff_m = 0.0;
    double pump_head_quadratic_coeff = 0.0;

    // Valve fields. opening_percent scales kv_max_m3_h linearly.
    // If opening is 0 and flow is positive, Kv loss becomes infinite.
    double opening_percent = 100.0;
    double kv_max_m3_h = 0.0;
    double kv_effective_m3_h = 0.0;
    double set_pressure_bar_g = 0.0;

    // Result fields populated after a solve.
    double flow_rate_m3_s = 0.0;
    double velocity_m_s = 0.0;
    double reynolds_number = 0.0;
    double friction_factor = 0.0;
    double friction_head_loss_m = 0.0;
    double local_head_loss_m = 0.0;
    double total_head_loss_m = 0.0;
    double pressure_loss_bar = 0.0;
    double pump_head_m = 0.0;
    double pump_delta_pressure_bar = 0.0;
    double elevation_change_m = 0.0;
    double static_pressure_change_bar = 0.0;
    std::string flow_regime;
    bool active = false;
    std::string active_from_node;
    std::string active_to_node;
    std::string flow_direction = "inactive";
};

struct Mode {
    // A mode defines which cells are active and which node is the load.
    // Examples:
    // - downstroke: pump -> P -> A -> cylinder cap, B -> T -> tank
    // - upstroke:   pump -> P -> B -> cylinder rod, A -> T -> tank
    // - relief:     pump -> relief valve -> tank
    // - pressure_hold: closed-center hold, zero flow, target pressure retained
    std::string name;
    std::string description;
    std::vector<std::string> supply_path;
    std::vector<std::string> return_path;
    std::string supply_start_node;
    std::string load_node;
    std::string return_start_node;
    std::string return_end_node;
    bool pressure_hold = false;
};

struct SolverSettings {
    // Flow search range for bisection. The solver searches for the flow where
    // supply path head equals the configured load node head.
    double flow_search_min_L_min = 10.0;
    double flow_search_max_L_min = 20.0;
};

struct PressSettings {
    // Optional target pressure applied to the active mode load node.
    // Realtime CSV rows update this value before each solve.
    bool has_target_pressure = false;
    double target_pressure_bar_g = 0.0;
};

class HydraulicTwinSolver {
public:
    bool load_network(const std::string& filename) {
        // Load and parse network.json or network.runtime.json.
        // The parser only reads known fields, so extra "_comment" fields in JSON
        // are safe and useful for documentation.
        std::ifstream file(filename);
        if (!file.is_open()) {
            std::cerr << "Failed to open " << filename << '\n';
            return false;
        }

        json root;
        file >> root;

        parse_fluid(root.at("fluid"));
        parse_solver_settings(root.value("solver", json::object()));
        parse_press_settings(root.value("press", json::object()));
        active_mode_ = root.value("active_mode", "downstroke");
        parse_nodes(root.at("nodes"));
        parse_cells(root.at("cells"));
        parse_modes(root.at("modes"));
        validate_mode(active_mode_);
        apply_press_target_pressure(modes_.at(active_mode_));
        return true;
    }

    void solve() {
        // Main solve sequence:
        // 1. Select active mode.
        // 2. Find flow rate unless this is a pressure-hold mode.
        // 3. Update all active cell results.
        // 4. Propagate pressure/head through supply and return paths.
        // 5. Print a text report; realtime mode parses this report into CSV.
        const Mode& mode = modes_.at(active_mode_);

        std::cout << "--- 1D Incompressible Navier-Stokes Hydraulic Solver ---\n";
        std::cout << "Model: node-cell network, Darcy-Weisbach + local losses + pump head\n";
        std::cout << "Fluid: " << fluid_.name
                  << " / rho=" << fluid_.density_kg_m3 << " kg/m3"
                  << " / T=" << fluid_.temperature_c << " C"
                  << " / mu=" << fluid_.viscosity_pa_s << " Pa.s\n";
        std::cout << "Active mode: " << mode.name << '\n';
        std::cout << "Description: " << mode.description << '\n';
        if (press_settings_.has_target_pressure && is_press_load_node(mode.load_node)) {
            std::cout << "Press target pressure: " << press_settings_.target_pressure_bar_g
                      << " bar(g) at " << mode.load_node << '\n';
        }

        // pressure_hold is a special closed-center state: no commanded flow.
        // The cap-side pressure is retained from Press.target_pressure_bar_g.
        double q_m3_s = mode.pressure_hold ? 0.0 : solve_flow_rate(mode);
        update_cell_results(q_m3_s, mode);
        if (mode.pressure_hold) {
            apply_pressure_hold_state(mode);
        } else {
            propagate_supply_path(mode, q_m3_s);
            propagate_return_path(mode, q_m3_s);
        }

        print_summary(mode, q_m3_s);
    }

private:
    Fluid fluid_;
    SolverSettings solver_settings_;
    PressSettings press_settings_;
    std::unordered_map<std::string, Node> nodes_;
    std::unordered_map<std::string, Cell> cells_;
    std::vector<std::string> cell_order_;
    std::unordered_map<std::string, Mode> modes_;
    std::string active_mode_;

    void parse_solver_settings(const json& j) {
        // The search bounds protect the solver from unrealistic flow values.
        // If the residual does not change sign, the closest bound is used and
        // a warning is printed.
        solver_settings_.flow_search_min_L_min = j.value("flow_search_min_L_min", 10.0);
        solver_settings_.flow_search_max_L_min = j.value("flow_search_max_L_min", 20.0);
        if (solver_settings_.flow_search_min_L_min < 0.0) {
            throw std::runtime_error("flow_search_min_L_min must be >= 0");
        }
        if (solver_settings_.flow_search_max_L_min <= solver_settings_.flow_search_min_L_min) {
            throw std::runtime_error("flow_search_max_L_min must be greater than flow_search_min_L_min");
        }
    }

    void parse_press_settings(const json& j) {
        // Press target pressure is optional for generic hydraulic networks, but
        // this project normally uses it to define the active cylinder load.
        if (!j.contains("target_pressure_bar_g")) {
            press_settings_.has_target_pressure = false;
            return;
        }
        press_settings_.has_target_pressure = true;
        press_settings_.target_pressure_bar_g = j.value("target_pressure_bar_g", 0.0);
        if (press_settings_.target_pressure_bar_g < 0.0) {
            throw std::runtime_error("press.target_pressure_bar_g must be >= 0");
        }
    }

    void parse_fluid(const json& j) {
        // network.json stores viscosity at 40 C. The runtime value is corrected
        // when Fluid.temperature_c is supplied by realtime input.
        fluid_.name = j.value("name", "hydraulic_oil");
        fluid_.density_kg_m3 = j.value("density_kg_m3", 860.0);
        fluid_.reference_temperature_c = 40.0;
        fluid_.reference_viscosity_pa_s = j.value("viscosity_pa_s", 0.0396);
        fluid_.temperature_c = j.value("temperature_c", fluid_.reference_temperature_c);
        fluid_.viscosity_pa_s = temperature_corrected_viscosity(
            fluid_.temperature_c,
            fluid_.reference_temperature_c,
            fluid_.reference_viscosity_pa_s);
        fluid_.gravity_m_s2 = j.value("gravity_m_s2", 9.80665);
    }

    void parse_nodes(const json& nodes_json) {
        // Nodes with pressure_bar_g are boundary/reference nodes at load time.
        // For example tank nodes are usually fixed at 0 bar(g).
        for (const auto& node_json : nodes_json) {
            Node node;
            node.id = node_json.at("id");
            node.name = node_json.value("name", "");
            node.type = node_json.value("type", "");
            node.elevation_m = node_json.value("elevation_m", 0.0);
            node.has_pressure_boundary = node_json.contains("pressure_bar_g");
            node.pressure_bar_g = node_json.value("pressure_bar_g", 0.0);
            update_node_head_from_pressure(node);
            nodes_[node.id] = node;
        }
    }

    void parse_cells(const json& cells_json) {
        // Cells are stored by id for quick lookup, while cell_order_ preserves
        // JSON order for stable printing.
        for (const auto& cell_json : cells_json) {
            Cell cell;
            cell.id = cell_json.at("id");
            cell.type = cell_json.at("type");
            cell.line_type = cell_json.value("line_type", "");
            cell.from_node = cell_json.at("from");
            cell.to_node = cell_json.at("to");

            cell.length_m = cell_json.value("length_m", 0.0);
            cell.diameter_m = cell_json.value("diameter_m", 0.0);
            cell.roughness_m = cell_json.value("roughness_m", 0.0);
            cell.local_loss_k = cell_json.value(
                "local_loss_k",
                cell_json.value("loss_coefficient_k", 0.0));
            cell.bend_count = cell_json.value("bend_count", 0);
            cell.allow_reverse_flow = cell_json.value(
                "allow_reverse_flow",
                cell.line_type == "A_port" || cell.line_type == "B_port");

            cell.is_running = cell_json.value("is_running", true);
            cell.rpm = cell_json.value("rpm", 0);
            cell.rated_rpm = cell_json.value("rated_rpm", cell.rpm);
            cell.pump_head_shutoff_m = cell_json.value("pump_head_shutoff_m", 2900.0);
            cell.pump_head_quadratic_coeff = cell_json.value("pump_head_quadratic_coeff", 100.0);

            cell.opening_percent = cell_json.value("opening_percent", 100.0);
            cell.kv_max_m3_h = cell_json.value("kv_max_m3_h", 0.0);
            cell.set_pressure_bar_g = cell_json.value("set_pressure_bar_g", 0.0);

            cells_[cell.id] = cell;
            cell_order_.push_back(cell.id);
        }
    }

    void parse_modes(const json& modes_json) {
        // Modes contain paths as ordered cell ids. Path order is important:
        // pressure/head is propagated from supply_start_node through this list.
        for (const auto& item : modes_json.items()) {
            const auto& mode_json = item.value();
            Mode mode;
            mode.name = item.key();
            mode.description = mode_json.value("description", "");
            mode.supply_path = mode_json.at("supply_path").get<std::vector<std::string>>();
            mode.return_path = mode_json.value("return_path", std::vector<std::string>{});
            mode.supply_start_node = mode_json.at("supply_start_node");
            mode.load_node = mode_json.at("load_node");
            mode.return_start_node = mode_json.value("return_start_node", "");
            mode.return_end_node = mode_json.value("return_end_node", "");
            mode.pressure_hold = mode_json.value("pressure_hold", false);
            modes_[mode.name] = mode;
        }
    }

    void validate_mode(const std::string& mode_name) const {
        // Validate only connectivity references. Detailed hydraulic validity
        // such as real-world valve overlap must be handled by model design.
        if (!modes_.contains(mode_name)) {
            throw std::runtime_error("active_mode not found: " + mode_name);
        }

        const Mode& mode = modes_.at(mode_name);
        validate_node(mode.load_node);
        if (!mode.supply_path.empty()) {
            validate_node(mode.supply_start_node);
        }
        for (const auto& cell_id : mode.supply_path) {
            validate_cell(cell_id);
        }
        for (const auto& cell_id : mode.return_path) {
            validate_cell(cell_id);
        }
        if (!mode.return_path.empty()) {
            validate_node(mode.return_start_node);
            validate_node(mode.return_end_node);
        }
    }

    void validate_node(const std::string& node_id) const {
        if (!nodes_.contains(node_id)) {
            throw std::runtime_error("node not found: " + node_id);
        }
    }

    void validate_cell(const std::string& cell_id) const {
        if (!cells_.contains(cell_id)) {
            throw std::runtime_error("cell not found: " + cell_id);
        }
    }

    static bool is_press_load_node(const std::string& node_id) {
        return node_id == "N_CYL_CAP" || node_id == "N_CYL_ROD";
    }

    void apply_press_target_pressure(const Mode& mode) {
        // Apply Press.target_pressure_bar_g to the active cylinder chamber.
        // downstroke targets N_CYL_CAP; upstroke targets N_CYL_ROD.
        if (!press_settings_.has_target_pressure || !is_press_load_node(mode.load_node)) {
            return;
        }

        Node& load_node = nodes_.at(mode.load_node);
        load_node.pressure_bar_g = press_settings_.target_pressure_bar_g;
        load_node.has_pressure_boundary = true;
        update_node_head_from_pressure(load_node);
    }

    void apply_pressure_hold_state(const Mode& mode) {
        // Closed-center hold approximation:
        // - Do not solve or propagate flow.
        // - Keep the active load node at the target pressure.
        // - Mirror the held chamber pressure to the adjacent valve port so the
        //   printed node table does not show a false pressure drop across a
        //   zero-flow closed line.
        if (!is_press_load_node(mode.load_node)) {
            return;
        }

        if (press_settings_.has_target_pressure) {
            apply_press_target_pressure(mode);
        }

        if (nodes_.contains("N_VALVE_A") && mode.load_node == "N_CYL_CAP") {
            update_node_from_head("N_VALVE_A", nodes_.at(mode.load_node).head_m);
        }
        if (nodes_.contains("N_VALVE_B") && mode.load_node == "N_CYL_ROD") {
            update_node_from_head("N_VALVE_B", nodes_.at(mode.load_node).head_m);
        }
    }

    void update_node_head_from_pressure(Node& node) const {
        // Hydraulic head is the solver's internal pressure representation.
        // head = elevation + pressure_head.
        node.pressure_bar_abs = node.pressure_bar_g + kAtmosphericPressureBar;
        node.head_m = node.elevation_m
            + bar_to_pa(node.pressure_bar_g) / (fluid_.density_kg_m3 * fluid_.gravity_m_s2);
    }

    void update_node_from_head(const std::string& node_id, double head_m) {
        // Convert propagated hydraulic head back to gauge and absolute pressure.
        Node& node = nodes_.at(node_id);
        node.head_m = head_m;
        node.pressure_bar_g = pressure_bar_from_head(head_m - node.elevation_m);
        node.pressure_bar_abs = node.pressure_bar_g + kAtmosphericPressureBar;
    }

    double solve_flow_rate(const Mode& mode) const {
        // Bisection flow solve:
        // residual(Q) = head available after supply path - required load head.
        // The root of residual(Q) is the flow where pump head and losses balance.
        double q_low = lpm_to_m3_s(solver_settings_.flow_search_min_L_min);
        double q_high = lpm_to_m3_s(solver_settings_.flow_search_max_L_min);
        double f_low = residual(mode, q_low);
        double f_high = residual(mode, q_high);

        if (f_low * f_high > 0.0) {
            double best_q = std::abs(f_low) <= std::abs(f_high) ? q_low : q_high;
            std::cout << "--- Solver warning ---\n";
            std::cout << "No residual sign change in "
                      << solver_settings_.flow_search_min_L_min << "-"
                      << solver_settings_.flow_search_max_L_min << " L/min. "
                      << "Using nearest bounded estimate.\n";
            return best_q;
        }

        double q_mid = 0.5 * (q_low + q_high);
        for (int iter = 0; iter < 100; ++iter) {
            q_mid = 0.5 * (q_low + q_high);
            double f_mid = residual(mode, q_mid);

            if (std::abs(f_mid) < 1e-7) {
                std::cout << "--- Convergence achieved in " << iter + 1 << " iterations ---\n";
                return q_mid;
            }

            if (f_low * f_mid <= 0.0) {
                q_high = q_mid;
                f_high = f_mid;
            } else {
                q_low = q_mid;
                f_low = f_mid;
            }
        }

        std::cout << "--- Bisection iteration limit reached ---\n";
        return q_mid;
    }

    double residual(const Mode& mode, double q_m3_s) const {
        // Calculate the pressure/head left after travelling through the supply
        // path at a candidate flow. Positive residual means available head is
        // higher than the target/load head.
        double start_head = nodes_.at(mode.supply_start_node).head_m;
        double load_head = nodes_.at(mode.load_node).head_m;
        double head = start_head;

        for (const auto& cell_id : mode.supply_path) {
            const Cell& cell = cells_.at(cell_id);
            if (cell.type == "pump") {
                head += pump_head(cell, q_m3_s);
            } else {
                head -= hydraulic_loss(cell, q_m3_s);
            }
        }

        return head - load_head;
    }

    void update_cell_results(double q_m3_s, const Mode& mode) {
        // Reset every cell, mark only active path cells, then calculate results
        // for active cells. Inactive branches report zero flow and inactive state.
        for (auto& item : cells_) {
            Cell& cell = item.second;
            cell.active = false;
            cell.active_from_node.clear();
            cell.active_to_node.clear();
            cell.flow_direction = "inactive";
        }

        mark_path_directions(mode.supply_path, mode.supply_start_node);
        if (!mode.return_path.empty()) {
            mark_path_directions(mode.return_path, mode.return_start_node);
        }

        for (auto& item : cells_) {
            Cell& cell = item.second;
            double active_q = cell.active ? q_m3_s : 0.0;
            cell.flow_rate_m3_s = active_q;
            cell.elevation_change_m = cell.active
                ? elevation_change(cell.active_from_node, cell.active_to_node)
                : elevation_change(cell.from_node, cell.to_node);
            cell.static_pressure_change_bar = -pressure_bar_from_head(cell.elevation_change_m);

            cell.velocity_m_s = 0.0;
            cell.reynolds_number = 0.0;
            cell.friction_factor = 0.0;
            cell.friction_head_loss_m = 0.0;
            cell.local_head_loss_m = 0.0;
            cell.kv_effective_m3_h = 0.0;
            cell.total_head_loss_m = 0.0;
            cell.pressure_loss_bar = 0.0;
            cell.pump_head_m = 0.0;
            cell.pump_delta_pressure_bar = 0.0;
            cell.flow_regime = "inactive";

            if (!cell.active) {
                continue;
            }

            if (cell.type == "pump") {
                // Pumps add head instead of consuming it.
                cell.pump_head_m = pump_head(cell, active_q);
                cell.pump_delta_pressure_bar = pressure_bar_from_head(cell.pump_head_m);
                continue;
            }

            if (cell.diameter_m <= 0.0) {
                continue;
            }

            cell.velocity_m_s = velocity(active_q, cell.diameter_m);
            cell.reynolds_number = reynolds(cell.velocity_m_s, cell.diameter_m);
            cell.friction_factor = friction_factor(cell.reynolds_number, cell.diameter_m, cell.roughness_m);

            if (cell.type == "pipe") {
                cell.friction_head_loss_m =
                    cell.friction_factor * (cell.length_m / cell.diameter_m)
                    * cell.velocity_m_s * cell.velocity_m_s / (2.0 * fluid_.gravity_m_s2);
            }

            if (cell.type == "valve" && cell.kv_max_m3_h > 0.0) {
                // Kv valve model is preferred when kv_max_m3_h exists.
                // opening_percent linearly scales effective Kv.
                cell.kv_effective_m3_h = effective_kv_m3_h(cell);
                cell.local_head_loss_m = valve_kv_loss_head(cell, active_q);
            } else {
                cell.local_head_loss_m =
                    cell.local_loss_k * cell.velocity_m_s * cell.velocity_m_s / (2.0 * fluid_.gravity_m_s2);
            }
            cell.total_head_loss_m = cell.friction_head_loss_m + cell.local_head_loss_m;
            cell.pressure_loss_bar = pressure_bar_from_head(cell.total_head_loss_m);
            cell.flow_regime = flow_regime(cell.reynolds_number);
        }
    }

    void propagate_supply_path(const Mode& mode, double q_m3_s) {
        // Starting from the supply boundary node, add pump head and subtract
        // pipe/valve losses until the load node is reached.
        if (mode.supply_path.empty()) {
            return;
        }

        std::string current_node = mode.supply_start_node;
        double current_head = nodes_.at(current_node).head_m;

        for (const auto& cell_id : mode.supply_path) {
            const Cell& cell = cells_.at(cell_id);
            std::string next_node = next_node_for_cell(cell, current_node);
            if (next_node.empty()) {
                throw std::runtime_error("broken supply path at cell: " + cell_id);
            }

            current_head += cell.type == "pump" ? pump_head(cell, q_m3_s) : -hydraulic_loss(cell, q_m3_s);
            update_node_from_head(next_node, current_head);
            current_node = next_node;
        }
    }

    void propagate_return_path(const Mode& mode, double q_m3_s) {
        // Return path is propagated from tank back toward the cylinder chamber.
        // This gives the non-pressurized chamber its return-line backpressure.
        if (mode.return_path.empty()) {
            return;
        }

        double return_loss = 0.0;
        for (const auto& cell_id : mode.return_path) {
            return_loss += hydraulic_loss(cells_.at(cell_id), q_m3_s);
        }

        double return_start_head = nodes_.at(mode.return_end_node).head_m + return_loss;
        update_node_from_head(mode.return_start_node, return_start_head);

        std::string current_node = mode.return_start_node;
        double current_head = return_start_head;
        for (const auto& cell_id : mode.return_path) {
            const Cell& cell = cells_.at(cell_id);
            std::string next_node = next_node_for_cell(cell, current_node);
            if (next_node.empty()) {
                throw std::runtime_error("broken return path at cell: " + cell_id);
            }

            current_head -= hydraulic_loss(cell, q_m3_s);
            update_node_from_head(next_node, current_head);
            current_node = next_node;
        }
    }

    std::string next_node_for_cell(const Cell& cell, const std::string& current_node) const {
        // Find the next node in a path. Reverse flow is allowed only for cells
        // explicitly configured with allow_reverse_flow=true.
        if (cell.from_node == current_node) {
            return cell.to_node;
        }
        if (cell.to_node == current_node) {
            if (!cell.allow_reverse_flow) {
                throw std::runtime_error("reverse flow is not allowed for cell: " + cell.id);
            }
            return cell.from_node;
        }
        return "";
    }

    void mark_path_directions(const std::vector<std::string>& path, const std::string& start_node) {
        // Store active direction metadata for result reporting and CSV parsing.
        std::string current_node = start_node;
        for (const auto& cell_id : path) {
            Cell& cell = cells_.at(cell_id);
            std::string next_node;

            if (cell.from_node == current_node) {
                next_node = cell.to_node;
                cell.flow_direction = "forward";
            } else if (cell.to_node == current_node) {
                if (!cell.allow_reverse_flow) {
                    throw std::runtime_error("reverse flow is not allowed for cell: " + cell.id);
                }
                next_node = cell.from_node;
                cell.flow_direction = "reverse";
            } else {
                throw std::runtime_error("path is not connected at cell: " + cell.id);
            }

            cell.active = true;
            cell.active_from_node = current_node;
            cell.active_to_node = next_node;
            current_node = next_node;
        }
    }

    double hydraulic_loss(const Cell& cell, double q_m3_s) const {
        // Common loss model for pipes and valves.
        // - Pipe: Darcy-Weisbach friction + configured local K loss.
        // - Valve with Kv: hydraulic loss from Kv equation.
        if (cell.type == "pump") {
            return 0.0;
        }
        if (cell.diameter_m <= 0.0) {
            return 0.0;
        }

        double v = velocity(q_m3_s, cell.diameter_m);
        double re = reynolds(v, cell.diameter_m);
        double f = friction_factor(re, cell.diameter_m, cell.roughness_m);
        double friction_loss = 0.0;

        if (cell.type == "pipe") {
            friction_loss = f * (cell.length_m / cell.diameter_m)
                * v * v / (2.0 * fluid_.gravity_m_s2);
        }

        if (cell.type == "valve" && cell.kv_max_m3_h > 0.0) {
            return valve_kv_loss_head(cell, q_m3_s);
        }

        double local_loss = cell.local_loss_k * v * v / (2.0 * fluid_.gravity_m_s2);
        return friction_loss + local_loss;
    }

    double effective_kv_m3_h(const Cell& cell) const {
        double opening_fraction = std::clamp(cell.opening_percent / 100.0, 0.0, 1.0);
        return cell.kv_max_m3_h * opening_fraction;
    }

    double valve_kv_loss_head(const Cell& cell, double q_m3_s) const {
        // Kv equation in metric units:
        // deltaP_bar = specific_gravity * (Q_m3_h / Kv)^2
        // The pressure loss is converted to head for the network solver.
        if (q_m3_s <= 0.0) {
            return 0.0;
        }

        double kv_effective = effective_kv_m3_h(cell);
        if (kv_effective <= 0.0) {
            return std::numeric_limits<double>::infinity();
        }

        double q_m3_h = q_m3_s * 3600.0;
        double specific_gravity = fluid_.density_kg_m3 / 1000.0;
        double delta_p_bar = specific_gravity * std::pow(q_m3_h / kv_effective, 2.0);
        return bar_to_pa(delta_p_bar) / (fluid_.density_kg_m3 * fluid_.gravity_m_s2);
    }

    double pump_head(const Cell& cell, double q_m3_s) const {
        // Simple variable-speed pump affinity law:
        // shutoff head scales with speed_ratio^2, and flow is converted to the
        // equivalent rated-speed flow before applying the quadratic curve.
        if (!cell.is_running || cell.type != "pump") {
            return 0.0;
        }
        if (cell.rpm <= 0 || cell.rated_rpm <= 0) {
            return 0.0;
        }

        double q_m3_h = q_m3_s * 3600.0;
        double speed_ratio = static_cast<double>(cell.rpm) / static_cast<double>(cell.rated_rpm);
        double equivalent_rated_q_m3_h = q_m3_h / speed_ratio;
        double rated_head_m = cell.pump_head_shutoff_m
            - cell.pump_head_quadratic_coeff * equivalent_rated_q_m3_h * equivalent_rated_q_m3_h;
        return std::max(0.0, rated_head_m * speed_ratio * speed_ratio);
    }

    double velocity(double q_m3_s, double diameter_m) const {
        double area = kPi * diameter_m * diameter_m / 4.0;
        return area > 0.0 ? q_m3_s / area : 0.0;
    }

    double reynolds(double v_m_s, double diameter_m) const {
        if (fluid_.viscosity_pa_s <= 0.0) {
            return 0.0;
        }
        return fluid_.density_kg_m3 * v_m_s * diameter_m / fluid_.viscosity_pa_s;
    }

    double friction_factor(double re, double diameter_m, double roughness_m) const {
        if (re <= 0.0) {
            return 0.0;
        }
        if (re < 2300.0) {
            return 64.0 / re;
        }

        double rr = roughness_m / diameter_m;
        double term = std::pow(rr / 3.7, 1.11) + 6.9 / re;
        return 1.0 / std::pow(-1.8 * std::log10(term), 2.0);
    }

    std::string flow_regime(double re) const {
        if (re <= 0.0) {
            return "inactive";
        }
        if (re < 2300.0) {
            return "laminar";
        }
        if (re < 4000.0) {
            return "transition";
        }
        return "turbulent";
    }

    double elevation_change(const std::string& from, const std::string& to) const {
        return nodes_.at(to).elevation_m - nodes_.at(from).elevation_m;
    }

    double pressure_bar_from_head(double head_m) const {
        return fluid_.density_kg_m3 * fluid_.gravity_m_s2 * head_m / 100000.0;
    }

    double temperature_corrected_viscosity(
        double temperature_c,
        double reference_temperature_c,
        double reference_viscosity_pa_s) const {
        // ISO VG hydraulic oils are strongly temperature-dependent. This simple
        // Andrade-type fit keeps the configured 40 C viscosity as the reference
        // and gives a practical digital-twin correction without adding a full
        // ASTM D341 viscosity table.
        constexpr double sensitivity = 0.025;
        return reference_viscosity_pa_s
            * std::exp(-sensitivity * (temperature_c - reference_temperature_c));
    }

    double lpm_to_m3_s(double lpm) const {
        return lpm / 1000.0 / 60.0;
    }

    double m3_s_to_lpm(double q_m3_s) const {
        return q_m3_s * 60000.0;
    }

    bool path_contains(const std::vector<std::string>& path, const std::string& cell_id) const {
        return std::find(path.begin(), path.end(), cell_id) != path.end();
    }

    std::string join_path(const std::vector<std::string>& path) const {
        std::ostringstream out;
        for (size_t i = 0; i < path.size(); ++i) {
            if (i > 0) {
                out << " -> ";
            }
            out << path[i];
        }
        return out.str();
    }

    void print_summary(const Mode& mode, double q_m3_s) const {
        std::cout << std::fixed << std::setprecision(4);
        std::cout << "Calculated flow: " << m3_s_to_lpm(q_m3_s)
                  << " L/min (" << q_m3_s * 3600.0 << " m3/h)\n";
        std::cout << "Temperature-corrected viscosity: " << fluid_.viscosity_pa_s
                  << " Pa.s at " << fluid_.temperature_c << " C"
                  << " (reference " << fluid_.reference_viscosity_pa_s
                  << " Pa.s at " << fluid_.reference_temperature_c << " C)\n";
        std::cout << "Supply path: " << join_path(mode.supply_path) << '\n';
        std::cout << "Return path : " << join_path(mode.return_path) << '\n';

        std::cout << "\n--- Node Results ---\n";
        std::vector<std::string> node_ids;
        node_ids.reserve(nodes_.size());
        for (const auto& item : nodes_) {
            node_ids.push_back(item.first);
        }
        std::sort(node_ids.begin(), node_ids.end());

        for (const auto& id : node_ids) {
            const Node& n = nodes_.at(id);
            std::cout << n.id
                      << " [" << n.type << "]"
                      << " elev=" << n.elevation_m << " m"
                      << ", head=" << n.head_m << " m"
                      << ", P=" << n.pressure_bar_g << " bar(g)"
                      << ", P_abs=" << n.pressure_bar_abs << " bar(abs)"
                      << '\n';
        }

        std::cout << "\n--- Cell Results ---\n";
        for (const auto& id : cell_order_) {
            const Cell& c = cells_.at(id);
            std::cout << c.id
                      << " [" << c.type << "/" << c.line_type << "]"
                      << " active=" << (c.active ? "yes" : "no")
                      << ", direction=" << c.flow_direction;
            if (c.active) {
                std::cout << "(" << c.active_from_node << "->" << c.active_to_node << ")";
            }
            std::cout
                      << ", flow=" << m3_s_to_lpm(c.flow_rate_m3_s) << " L/min";

            if (c.type == "pump") {
                std::cout << ", pump_head=" << c.pump_head_m << " m"
                          << ", deltaP=" << c.pump_delta_pressure_bar << " bar"
                          << ", rpm=" << c.rpm
                          << ", rated_rpm=" << c.rated_rpm;
            } else {
                std::cout << ", velocity=" << c.velocity_m_s << " m/s"
                          << ", Re=" << c.reynolds_number
                          << ", regime=" << c.flow_regime
                          << ", friction_loss=" << c.friction_head_loss_m << " m"
                          << ", local_loss=" << c.local_head_loss_m << " m"
                          << ", total_loss=" << c.total_head_loss_m << " m"
                          << ", pressure_loss=" << c.pressure_loss_bar << " bar";
                if (c.type == "valve" && c.kv_max_m3_h > 0.0) {
                    std::cout << ", Kv_max=" << c.kv_max_m3_h << " m3/h"
                              << ", Kv_effective=" << c.kv_effective_m3_h << " m3/h"
                              << ", opening=" << c.opening_percent << " %";
                }
            }

            std::cout << ", dz=" << c.elevation_change_m << " m"
                      << ", static_dP=" << c.static_pressure_change_bar << " bar"
                      << '\n';
        }

        print_suction_check();
        print_relief_check();
    }

    void print_suction_check() const {
        auto pump_it = std::find_if(cells_.begin(), cells_.end(), [](const auto& item) {
            return item.second.type == "pump";
        });
        if (pump_it == cells_.end()) {
            return;
        }

        const Node& inlet = nodes_.at(pump_it->second.from_node);
        double pressure_head_abs =
            bar_to_pa(inlet.pressure_bar_abs) / (fluid_.density_kg_m3 * fluid_.gravity_m_s2);
        double npsh_available_m = pressure_head_abs + inlet.elevation_m;

        std::cout << "\n--- Suction Check ---\n";
        std::cout << "Pump inlet: " << inlet.id
                  << ", P_abs=" << inlet.pressure_bar_abs << " bar(abs)"
                  << ", NPSH_available~=" << npsh_available_m << " m\n";
    }

    void print_relief_check() const {
        const Cell* relief = nullptr;
        for (const auto& item : cells_) {
            if (item.second.line_type == "relief_valve") {
                relief = &item.second;
                break;
            }
        }
        if (!relief || relief->set_pressure_bar_g <= 0.0 || !nodes_.contains("N_VALVE_P")) {
            return;
        }

        const Node& p_port = nodes_.at("N_VALVE_P");
        bool open = p_port.pressure_bar_g >= relief->set_pressure_bar_g;

        std::cout << "\n--- Relief Check ---\n";
        std::cout << "P-port pressure=" << p_port.pressure_bar_g
                  << " bar(g), relief set=" << relief->set_pressure_bar_g
                  << " bar(g), relief_open=" << (open ? "yes" : "no") << '\n';
    }
};

struct SolverTextResult {
    // Parsed values from the solver's text report. The realtime wrapper keeps
    // the solver print format as the single reporting source, then extracts the
    // values needed for CSV output.
    std::unordered_map<std::string, double> nodes;
    std::unordered_map<std::string, double> cell_velocity;
    std::unordered_map<std::string, double> cell_pressure_loss;
    std::unordered_map<std::string, double> cell_kv_effective;
    std::unordered_map<std::string, double> cell_pump_head;
    std::unordered_map<std::string, double> cell_pump_delta_p;
    double calculated_flow_rate_L_min = 0.0;
    double calculated_flow_rate_m3_h = 0.0;
    bool solver_warning = false;
    bool contains_inf_or_nan = false;
};

using CsvRow = std::unordered_map<std::string, std::string>;

std::string trim_copy(const std::string& text) {
    // CSV values often include spaces or CR/LF at the end of a line.
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return "";
    }
    const auto last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

std::string lower_copy(std::string text) {
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return text;
}

bool truthy(const std::string& text) {
    // Accept common PLC/CSV boolean spellings for pump running state.
    const std::string value = lower_copy(trim_copy(text));
    return value == "true" || value == "yes" || value == "1" || value == "running" || value == "on";
}

double to_double_or(const std::string& text, double fallback = 0.0) {
    // Realtime CSV should not crash the process because one numeric field is
    // blank or malformed. A fallback keeps the loop alive.
    try {
        size_t pos = 0;
        const double value = std::stod(trim_copy(text), &pos);
        return pos > 0 ? value : fallback;
    } catch (...) {
        return fallback;
    }
}

std::vector<std::string> parse_csv_line(const std::string& line) {
    // Minimal CSV parser supporting quoted fields and escaped quotes.
    // This avoids adding a dependency for realtime deployments.
    std::vector<std::string> fields;
    std::string current;
    bool in_quotes = false;

    for (size_t i = 0; i < line.size(); ++i) {
        const char ch = line[i];
        if (ch == '"') {
            if (in_quotes && i + 1 < line.size() && line[i + 1] == '"') {
                current.push_back('"');
                ++i;
            } else {
                in_quotes = !in_quotes;
            }
        } else if (ch == ',' && !in_quotes) {
            fields.push_back(current);
            current.clear();
        } else {
            current.push_back(ch);
        }
    }
    fields.push_back(current);
    return fields;
}

std::vector<CsvRow> read_csv_rows(const std::filesystem::path& path) {
    // Read the whole CSV every polling cycle. The wrapper processes only rows
    // after processed_rows, so this is simple and works well for small realtime
    // append-style files.
    std::ifstream file(path);
    if (!file.is_open()) {
        return {};
    }

    std::string header_line;
    if (!std::getline(file, header_line)) {
        return {};
    }

    auto headers = parse_csv_line(header_line);
    if (!headers.empty() && headers.front().size() >= 3
        && static_cast<unsigned char>(headers.front()[0]) == 0xEF
        && static_cast<unsigned char>(headers.front()[1]) == 0xBB
        && static_cast<unsigned char>(headers.front()[2]) == 0xBF) {
        headers.front() = headers.front().substr(3);
    }

    std::vector<CsvRow> rows;
    std::string line;
    while (std::getline(file, line)) {
        if (trim_copy(line).empty()) {
            continue;
        }
        auto fields = parse_csv_line(line);
        CsvRow row;
        for (size_t i = 0; i < headers.size(); ++i) {
            row[headers[i]] = i < fields.size() ? fields[i] : "";
        }
        rows.push_back(std::move(row));
    }
    return rows;
}

std::string csv_escape(const std::string& value) {
    // Output values are quoted only when needed. Excel can read the generated
    // UTF-8 BOM CSV directly.
    bool must_quote = value.find_first_of(",\"\r\n") != std::string::npos;
    if (!must_quote) {
        return value;
    }

    std::string escaped = "\"";
    for (char ch : value) {
        if (ch == '"') {
            escaped += "\"\"";
        } else {
            escaped.push_back(ch);
        }
    }
    escaped.push_back('"');
    return escaped;
}

std::string row_value(const CsvRow& row, const std::string& key, const std::string& fallback = "") {
    auto it = row.find(key);
    return it == row.end() || it->second.empty() ? fallback : it->second;
}

json* json_item_by_id(json& array, const std::string& id) {
    // network.json stores nodes and cells in arrays. This helper finds one item
    // by its "id" field so a CSV row can update only the relevant component.
    for (auto& item : array) {
        if (item.value("id", "") == id) {
            return &item;
        }
    }
    return nullptr;
}

void set_node_pressure(json& network, const std::string& node_id, double pressure_bar_g) {
    if (json* node = json_item_by_id(network["nodes"], node_id)) {
        (*node)["pressure_bar_g"] = pressure_bar_g;
    }
}

void set_cell_number(json& network, const std::string& cell_id, const std::string& property, double value) {
    if (json* cell = json_item_by_id(network["cells"], cell_id)) {
        (*cell)[property] = value;
    }
}

void set_cell_bool(json& network, const std::string& cell_id, const std::string& property, bool value) {
    if (json* cell = json_item_by_id(network["cells"], cell_id)) {
        (*cell)[property] = value;
    }
}

void apply_input_row(json& network, const CsvRow& row) {
    // Map one realtime input row into the network model.
    // Supported input column examples:
    // - System.active_mode
    // - Press.target_pressure_bar_g
    // - Node.N_CYL_CAP.pressure_bar_g
    // - Cell.PUMP_01.rpm
    // - Cell.V_DIR_PA.opening_percent
    //
    // Missing fields are allowed. The base network.json value is kept when an
    // input column is not present.
    network["active_mode"] = row_value(row, "System.active_mode", row_value(row, "active_mode", "downstroke"));

    if (!network.contains("press") || !network["press"].is_object()) {
        network["press"] = json::object();
    }
    network["press"]["target_pressure_bar_g"] = to_double_or(
        row_value(row, "Press.target_pressure_bar_g", row_value(row, "Press_target_pressure_bar_g", "230.0")),
        230.0);

    if (network.contains("fluid")) {
        const std::string temperature = row_value(row, "Fluid.temperature_c");
        if (!temperature.empty()) {
            network["fluid"]["temperature_c"] = to_double_or(temperature, network["fluid"].value("temperature_c", 40.0));
        }
    }

    for (const std::string& node_id : {
        "N_CYL_CAP", "N_CYL_ROD", "N_TANK_SUCTION", "N_TANK_RETURN", "N_TANK_RELIEF"
    }) {
        const std::string key = "Node." + node_id + ".pressure_bar_g";
        const std::string value = row_value(row, key);
        if (!value.empty()) {
            set_node_pressure(network, node_id, to_double_or(value));
        }
    }

    set_cell_bool(network, "PUMP_01", "is_running", truthy(row_value(row, "Cell.PUMP_01.is_running", "true")));
    set_cell_number(network, "PUMP_01", "rpm", to_double_or(row_value(row, "Cell.PUMP_01.rpm", "1800"), 1800.0));
    set_cell_number(network, "PUMP_01", "rated_rpm", to_double_or(row_value(row, "Cell.PUMP_01.rated_rpm", "1800"), 1800.0));

    for (const std::string& cell_id : {"V_DIR_PA", "V_DIR_PB", "V_DIR_AT", "V_DIR_BT", "V_RELIEF"}) {
        const std::string key = "Cell." + cell_id + ".opening_percent";
        const std::string value = row_value(row, key);
        if (!value.empty()) {
            set_cell_number(network, cell_id, "opening_percent", to_double_or(value));
        }
    }

    const std::string relief_set = row_value(row, "Cell.V_RELIEF.set_pressure_bar_g");
    if (!relief_set.empty()) {
        set_cell_number(network, "V_RELIEF", "set_pressure_bar_g", to_double_or(relief_set));
    }
}

SolverTextResult parse_solver_text(const std::string& text) {
    // Convert the human-readable solver report into structured values for CSV.
    // The regex patterns intentionally target stable labels such as:
    // "Calculated flow:", "P=... bar(g)", "velocity=", and "pressure_loss=".
    SolverTextResult result;
    result.solver_warning = text.find("--- Solver warning ---") != std::string::npos;
    const std::string lowered = lower_copy(text);
    result.contains_inf_or_nan = lowered.find("inf") != std::string::npos || lowered.find("nan") != std::string::npos;

    std::regex flow_re(R"(Calculated flow:\s*([-+0-9.Ee]+)\s*L/min\s*\(([-+0-9.Ee]+)\s*m3/h\))");
    std::regex node_re(R"(^\s*(N_[A-Z0-9_]+)\s+\[[^\]]+\].*P=([-+0-9.Ee]+)\s*bar\(g\))");
    std::regex cell_re(R"(^\s*([A-Z0-9_]+)\s+\[[^\]]+\].*)");
    std::regex value_re(R"((velocity|pressure_loss|Kv_effective|pump_head|deltaP)=([-+0-9.Ee]+))");

    std::smatch match;
    if (std::regex_search(text, match, flow_re)) {
        result.calculated_flow_rate_L_min = to_double_or(match[1].str());
        result.calculated_flow_rate_m3_h = to_double_or(match[2].str());
    }

    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines, line)) {
        if (std::regex_search(line, match, node_re)) {
            result.nodes[match[1].str()] = to_double_or(match[2].str());
            continue;
        }

        if (std::regex_search(line, match, cell_re)) {
            const std::string cell_id = match[1].str();
            auto begin = std::sregex_iterator(line.begin(), line.end(), value_re);
            auto end = std::sregex_iterator();
            for (auto it = begin; it != end; ++it) {
                const std::string name = (*it)[1].str();
                const double value = to_double_or((*it)[2].str());
                if (name == "velocity") {
                    result.cell_velocity[cell_id] = value;
                } else if (name == "pressure_loss") {
                    result.cell_pressure_loss[cell_id] = value;
                } else if (name == "Kv_effective") {
                    result.cell_kv_effective[cell_id] = value;
                } else if (name == "pump_head") {
                    result.cell_pump_head[cell_id] = value;
                } else if (name == "deltaP") {
                    result.cell_pump_delta_p[cell_id] = value;
                }
            }
        }
    }

    return result;
}

std::string run_solver_to_text(const std::filesystem::path& network_path) {
    // Run the existing solver without changing its public API. stdout is
    // temporarily redirected into a string so the realtime wrapper can parse it.
    HydraulicTwinSolver solver;
    if (!solver.load_network(network_path.string())) {
        throw std::runtime_error("failed to load runtime network: " + network_path.string());
    }

    std::ostringstream captured;
    auto* old_buffer = std::cout.rdbuf(captured.rdbuf());
    solver.solve();
    std::cout.rdbuf(old_buffer);
    return captured.str();
}

double map_get_double(const std::unordered_map<std::string, double>& values, const std::string& key) {
    auto it = values.find(key);
    return it == values.end() ? 0.0 : it->second;
}

std::string fixed4(double value) {
    std::ostringstream out;
    out << std::fixed << std::setprecision(4) << value;
    return out.str();
}

std::vector<std::string> realtime_output_header() {
    // Output CSV columns. These are the values normally useful for a dashboard,
    // anomaly detection, or a later predictive-maintenance model.
    return {
        "processed_at", "input_row_index", "timestamp", "cycle_id", "cycle_phase", "active_mode",
        "Press_target_pressure_bar_g", "Press_target_chamber", "target_pressure_error_bar", "load_pressure_bar_g",
        "calculated_flow_rate_L_min", "calculated_flow_rate_m3_h", "hydraulic_power_kW",
        "pump_head_m", "pump_delta_pressure_bar",
        "N_PUMP_IN_pressure_bar_g", "N_PUMP_OUT_pressure_bar_g", "N_VALVE_P_pressure_bar_g",
        "N_VALVE_A_pressure_bar_g", "N_VALVE_B_pressure_bar_g", "N_VALVE_T_pressure_bar_g",
        "N_CYL_CAP_pressure_bar_g", "N_CYL_ROD_pressure_bar_g",
        "C_SUCTION_velocity_m_s", "C_PRESSURE_velocity_m_s", "C_A_LINE_velocity_m_s", "C_B_LINE_velocity_m_s",
        "C_RETURN_velocity_m_s", "V_RELIEF_velocity_m_s", "C_RELIEF_velocity_m_s", "max_active_velocity_m_s",
        "C_SUCTION_pressure_loss_bar", "C_PRESSURE_pressure_loss_bar", "C_A_LINE_pressure_loss_bar",
        "C_B_LINE_pressure_loss_bar", "C_RETURN_pressure_loss_bar", "V_RELIEF_pressure_loss_bar",
        "V_DIR_PA_Kv_effective_m3_h", "V_DIR_PB_Kv_effective_m3_h", "V_DIR_AT_Kv_effective_m3_h",
        "V_DIR_BT_Kv_effective_m3_h", "V_RELIEF_Kv_effective_m3_h",
        "solver_warning", "contains_inf_or_nan", "status"
    };
}

std::string local_time_string() {
    const auto now = std::chrono::system_clock::now();
    const std::time_t raw_time = std::chrono::system_clock::to_time_t(now);
    std::tm local_tm{};
#ifdef _WIN32
    localtime_s(&local_tm, &raw_time);
#else
    localtime_r(&raw_time, &local_tm);
#endif
    std::ostringstream out;
    out << std::put_time(&local_tm, "%Y-%m-%d %H:%M:%S");
    return out.str();
}

void append_output_row(const std::filesystem::path& output_path, const std::vector<std::string>& values) {
    // Create the output CSV if needed, otherwise append one new result row.
    // Requirement: each new run starts by deleting the previous output CSV in
    // run_csv_solver(), so this file normally contains only the latest run.
    const bool write_header = !std::filesystem::exists(output_path) || std::filesystem::file_size(output_path) == 0;
    std::ofstream file(output_path, std::ios::app);
    if (!file.is_open()) {
        throw std::runtime_error("failed to open output csv: " + output_path.string());
    }

    const auto header = realtime_output_header();
    if (write_header) {
        file << "\xEF\xBB\xBF";
        for (size_t i = 0; i < header.size(); ++i) {
            if (i > 0) {
                file << ',';
            }
            file << csv_escape(header[i]);
        }
        file << '\n';
    }

    for (size_t i = 0; i < values.size(); ++i) {
        if (i > 0) {
            file << ',';
        }
        file << csv_escape(values[i]);
    }
    file << '\n';
}

std::vector<std::string> build_output_row(
    const CsvRow& input,
    size_t row_index,
    const SolverTextResult& result,
    const std::string& status) {
    // Build one output CSV row from:
    // - Original input row metadata.
    // - Parsed solver results.
    // - Derived values such as target pressure error and hydraulic power.
    const std::string active_mode = row_value(input, "System.active_mode", row_value(input, "active_mode", "downstroke"));
    const std::string target_text = row_value(input, "Press.target_pressure_bar_g", row_value(input, "Press_target_pressure_bar_g", "0"));
    const double target_pressure = to_double_or(target_text);
    const std::string target_chamber = row_value(input, "Press.target_chamber", row_value(input, "Press_target_chamber", active_mode == "upstroke" ? "rod" : "cap"));

    const std::string load_node =
        target_chamber == "rod" ? "N_CYL_ROD"
        : target_chamber == "cap" ? "N_CYL_CAP"
        : active_mode == "upstroke" ? "N_CYL_ROD"
        : active_mode == "relief" ? "N_TANK_RELIEF"
        : "N_CYL_CAP";
    const double load_pressure = map_get_double(result.nodes, load_node);
    const double target_error = std::abs(target_pressure - load_pressure);
    const double pump_delta_p = map_get_double(result.cell_pump_delta_p, "PUMP_01");
    const double pump_head = map_get_double(result.cell_pump_head, "PUMP_01");
    const double hydraulic_power = pump_delta_p * result.calculated_flow_rate_L_min / 600.0;

    double max_velocity = 0.0;
    for (const std::string& cell_id : {
        "C_SUCTION", "C_PRESSURE", "C_A_LINE", "C_B_LINE", "C_RETURN", "V_RELIEF", "C_RELIEF"
    }) {
        max_velocity = std::max(max_velocity, std::abs(map_get_double(result.cell_velocity, cell_id)));
    }

    return {
        local_time_string(),
        std::to_string(row_index + 1),
        row_value(input, "timestamp"),
        row_value(input, "cycle_id"),
        row_value(input, "cycle_phase"),
        active_mode,
        fixed4(target_pressure),
        target_chamber,
        fixed4(target_error),
        fixed4(load_pressure),
        fixed4(result.calculated_flow_rate_L_min),
        fixed4(result.calculated_flow_rate_m3_h),
        fixed4(hydraulic_power),
        fixed4(pump_head),
        fixed4(pump_delta_p),
        fixed4(map_get_double(result.nodes, "N_PUMP_IN")),
        fixed4(map_get_double(result.nodes, "N_PUMP_OUT")),
        fixed4(map_get_double(result.nodes, "N_VALVE_P")),
        fixed4(map_get_double(result.nodes, "N_VALVE_A")),
        fixed4(map_get_double(result.nodes, "N_VALVE_B")),
        fixed4(map_get_double(result.nodes, "N_VALVE_T")),
        fixed4(map_get_double(result.nodes, "N_CYL_CAP")),
        fixed4(map_get_double(result.nodes, "N_CYL_ROD")),
        fixed4(map_get_double(result.cell_velocity, "C_SUCTION")),
        fixed4(map_get_double(result.cell_velocity, "C_PRESSURE")),
        fixed4(map_get_double(result.cell_velocity, "C_A_LINE")),
        fixed4(map_get_double(result.cell_velocity, "C_B_LINE")),
        fixed4(map_get_double(result.cell_velocity, "C_RETURN")),
        fixed4(map_get_double(result.cell_velocity, "V_RELIEF")),
        fixed4(map_get_double(result.cell_velocity, "C_RELIEF")),
        fixed4(max_velocity),
        fixed4(map_get_double(result.cell_pressure_loss, "C_SUCTION")),
        fixed4(map_get_double(result.cell_pressure_loss, "C_PRESSURE")),
        fixed4(map_get_double(result.cell_pressure_loss, "C_A_LINE")),
        fixed4(map_get_double(result.cell_pressure_loss, "C_B_LINE")),
        fixed4(map_get_double(result.cell_pressure_loss, "C_RETURN")),
        fixed4(map_get_double(result.cell_pressure_loss, "V_RELIEF")),
        fixed4(map_get_double(result.cell_kv_effective, "V_DIR_PA")),
        fixed4(map_get_double(result.cell_kv_effective, "V_DIR_PB")),
        fixed4(map_get_double(result.cell_kv_effective, "V_DIR_AT")),
        fixed4(map_get_double(result.cell_kv_effective, "V_DIR_BT")),
        fixed4(map_get_double(result.cell_kv_effective, "V_RELIEF")),
        result.solver_warning ? "yes" : "no",
        result.contains_inf_or_nan ? "yes" : "no",
        status
    };
}

int run_csv_solver(bool watch, const std::filesystem::path& input_path, const std::filesystem::path& output_path, int poll_ms) {
    // CSV realtime execution loop.
    //
    // --once:
    //   Read all rows currently present in input_path, solve them, write output,
    //   then exit.
    //
    // --realtime:
    //   Keep polling input_path. Only newly appended rows are solved. This fits
    //   a PLC/DAQ process that appends one sensor row every second.
    const std::filesystem::path base_network = "network.json";
    const std::filesystem::path runtime_network = "network.runtime.json";

    if (std::filesystem::exists(output_path)) {
        // Requirement from the workflow: remove previous CSV output whenever a
        // new requirement/run creates fresh CSV results.
        std::filesystem::remove(output_path);
    }

    size_t processed_rows = 0;
    std::cout << "Realtime CSV solver started.\n"
              << "Input : " << input_path.string() << '\n'
              << "Output: " << output_path.string() << '\n'
              << "Mode  : " << (watch ? "watch" : "once") << '\n';

    while (true) {
        const auto rows = read_csv_rows(input_path);
        if (rows.size() < processed_rows) {
            processed_rows = 0;
        }

        if (!rows.empty()) {
            std::ifstream base_file(base_network);
            if (!base_file.is_open()) {
                throw std::runtime_error("failed to open network.json");
            }
            json base_network_json;
            base_file >> base_network_json;

            for (size_t i = processed_rows; i < rows.size(); ++i) {
                // Start from the clean base model for every input row so values
                // from one row cannot accidentally leak into the next row.
                json runtime = base_network_json;
                apply_input_row(runtime, rows[i]);

                std::ofstream runtime_file(runtime_network);
                // Temporary network file makes the ordinary solver path reusable.
                runtime_file << std::setw(2) << runtime << '\n';
                runtime_file.close();

                const auto solve_started_at = std::chrono::steady_clock::now();
                const std::string solver_text = run_solver_to_text(runtime_network);
                const SolverTextResult parsed = parse_solver_text(solver_text);
                append_output_row(output_path, build_output_row(rows[i], i, parsed, "ok"));
                const auto solve_elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - solve_started_at).count();
                std::cout
                    << "[SOLVER_COMPLETED] row=" << i + 1
                    << " cycle_id=" << row_value(rows[i], "cycle_id")
                    << " timestamp=" << row_value(rows[i], "timestamp")
                    << " elapsed_ms=" << solve_elapsed_ms
                    << " status=ok"
                    << " output=" << output_path.string()
                    << std::endl;
            }
            processed_rows = rows.size();
        }

        if (!watch) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(std::max(100, poll_ms)));
    }

    if (std::filesystem::exists(runtime_network)) {
        std::filesystem::remove(runtime_network);
    }
    return 0;
}

void print_usage() {
    // Command-line help shown by --help or by invalid options.
    std::cout
        << "Usage:\n"
        << "  Project1.exe\n"
        << "  Project1.exe --once [input.csv] [output.csv]\n"
        << "  Project1.exe --realtime [input.csv] [output.csv] [poll_ms]\n\n"
        << "Default realtime input : realtime_input.csv\n"
        << "Default realtime output: realtime_solver_output.csv\n";
}

int main(int argc, char* argv[]) {
    try {
        // No arguments: preserve the original project behavior.
        // --once / --realtime: use the CSV wrapper around the same solver.
        if (argc > 1) {
            const std::string mode = argv[1];
            if (mode == "--help" || mode == "-h") {
                print_usage();
                return 0;
            }
            if (mode == "--once" || mode == "--realtime") {
                const bool watch = mode == "--realtime";
                const std::filesystem::path input_path = argc > 2 ? argv[2] : "realtime_input.csv";
                const std::filesystem::path output_path = argc > 3 ? argv[3] : "realtime_solver_output.csv";
                const int poll_ms = argc > 4 ? static_cast<int>(to_double_or(argv[4], 1000.0)) : 1000;
                return run_csv_solver(watch, input_path, output_path, poll_ms);
            }
            std::cerr << "Unknown option: " << mode << "\n\n";
            print_usage();
            return 2;
        }

        HydraulicTwinSolver solver;
        if (!solver.load_network("network.json")) {
            return 1;
        }
        solver.solve();
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "Solver error: " << ex.what() << '\n';
        return 1;
    }
}
