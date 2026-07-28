#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "json.hpp"

using json = nlohmann::json;

namespace {
constexpr double kAtmosphericPressureBar = 1.01325;
constexpr double kPi = 3.14159265358979323846;

double bar_to_pa(double bar) {
    return bar * 100000.0;
}
}

struct Fluid {
    std::string name;
    double density_kg_m3 = 860.0;
    double viscosity_pa_s = 0.0396;
    double reference_temperature_c = 40.0;
    double reference_viscosity_pa_s = 0.0396;
    double temperature_c = 40.0;
    double gravity_m_s2 = 9.80665;
};

struct Node {
    std::string id;
    std::string name;
    std::string type;
    double elevation_m = 0.0;
    double pressure_bar_g = 0.0;
    double pressure_bar_abs = kAtmosphericPressureBar;
    double head_m = 0.0;
    bool has_pressure_boundary = false;
};

struct Cell {
    std::string id;
    std::string type;
    std::string line_type;
    std::string from_node;
    std::string to_node;

    double length_m = 0.0;
    double diameter_m = 0.0;
    double roughness_m = 0.0;
    double local_loss_k = 0.0;
    int bend_count = 0;
    bool allow_reverse_flow = false;

    bool is_running = true;
    int rpm = 0;
    int rated_rpm = 0;
    double pump_head_shutoff_m = 0.0;
    double pump_head_quadratic_coeff = 0.0;

    double opening_percent = 100.0;
    double kv_max_m3_h = 0.0;
    double kv_effective_m3_h = 0.0;
    double set_pressure_bar_g = 0.0;

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
    double flow_search_min_L_min = 10.0;
    double flow_search_max_L_min = 20.0;
};

struct PressSettings {
    bool has_target_pressure = false;
    double target_pressure_bar_g = 0.0;
};

class HydraulicTwinSolver {
public:
    bool load_network(const std::string& filename) {
        std::ifstream file(filename);
        if (!file.is_open()) {
            std::cerr << "Failed to open " << filename << '\n';
            return false;
        }
        json root; file >> root;
        return load_from_json(root);
    }

    bool load_from_json(const json& root) {
        nodes_.clear(); cells_.clear(); cell_order_.clear(); modes_.clear();
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

    // ---- batch driver accessors ----
    bool quiet_ = true;
    double last_q_m3_s_ = 0.0;
    double leak_K_ = 0.0; std::string leak_from_, leak_to_; double load_cap_ = 1e9;
    double nodeP(const std::string& id) const { auto it=nodes_.find(id); return it==nodes_.end()?0.0:it->second.pressure_bar_g; }
    bool hasNode(const std::string& id) const { return nodes_.count(id)>0; }
    const Cell* cellPtr(const std::string& id) const { auto it=cells_.find(id); return it==cells_.end()?nullptr:&it->second; }
    double viscosity() const { return fluid_.viscosity_pa_s; }
    const Mode& activeMode() const { return modes_.at(active_mode_); }
    double lastQ() const { return last_q_m3_s_; }
    double maxActiveVelocity() const { double m=0; for(auto&kv:cells_) if(kv.second.active) m=std::max(m,kv.second.velocity_m_s); return m; }

    void solve() {
        const Mode& mode = modes_.at(active_mode_);
        // fault: relief cap / overpressure -> cap or lift load-node pressure boundary
        if (is_press_load_node(mode.load_node) && nodes_.count(mode.load_node)) {
            Node& ln = nodes_.at(mode.load_node);
            if (ln.pressure_bar_g > load_cap_) { ln.pressure_bar_g = load_cap_; update_node_head_from_pressure(ln); }
        }

        if(!quiet_) std::cout << "--- 1D Incompressible Navier-Stokes Hydraulic Solver ---\n";
        if(!quiet_) std::cout << "Model: node-cell network, Darcy-Weisbach + local losses + pump head\n";
        if(!quiet_) std::cout << "Fluid: " << fluid_.name
                  << " / rho=" << fluid_.density_kg_m3 << " kg/m3"
                  << " / T=" << fluid_.temperature_c << " C"
                  << " / mu=" << fluid_.viscosity_pa_s << " Pa.s\n";
        if(!quiet_) std::cout << "Active mode: " << mode.name << '\n';
        if(!quiet_) std::cout << "Description: " << mode.description << '\n';
        if (press_settings_.has_target_pressure && is_press_load_node(mode.load_node)) {
            if(!quiet_) std::cout << "Press target pressure: " << press_settings_.target_pressure_bar_g
                      << " bar(g) at " << mode.load_node << '\n';
        }

        double q_m3_s = mode.pressure_hold ? 0.0 : solve_flow_rate(mode);
        last_q_m3_s_ = q_m3_s;
        update_cell_results(q_m3_s, mode);
        if (mode.pressure_hold) {
            apply_pressure_hold_state(mode);
        } else {
            propagate_supply_path(mode, q_m3_s);
            propagate_return_path(mode, q_m3_s);
        }

        if (leak_K_ > 0.0 && !mode.pressure_hold) {
            std::string lf = leak_from_.empty() ? mode.load_node : leak_from_;
            double pf = nodes_.count(lf) ? nodes_.at(lf).pressure_bar_g : 0.0;
            double pt = (!leak_to_.empty() && nodes_.count(leak_to_)) ? nodes_.at(leak_to_).pressure_bar_g : 0.0;
            double dp_pa = (pf - pt) * 100000.0; if (dp_pa < 0.0) dp_pa = 0.0;
            double q_leak = leak_K_ * dp_pa;
            double q_total = q_m3_s + q_leak;
            last_q_m3_s_ = q_total;
            update_cell_results(q_total, mode);
            propagate_supply_path(mode, q_total);

            // leak_to_가 비어있으면(ext_leak) 누설분이 회로 밖(대기/탱크 외부)으로 완전히
            // 빠져나가 리턴경로까지 도달하지 못하므로, 리턴경로는 누설분을 뺀 기본유량(q_m3_s)만
            // 반영해야 한다. leak_to_가 지정된 경우(valve_int_leak -> N_VALVE_T, seal_leak ->
            // N_CYL_ROD)는 누설분이 여전히 회로 내부에 남아 리턴경로에 정당하게 합류하므로
            // 기존과 동일하게 q_total을 사용한다.
            bool leak_is_external = leak_to_.empty();
            double q_return = leak_is_external ? q_m3_s : q_total;

            propagate_return_path(mode, q_return);
            reapply_flow_to_path(mode.return_path, q_return);
        }
        if(!quiet_) print_summary(mode, q_m3_s);
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
        if (!press_settings_.has_target_pressure || !is_press_load_node(mode.load_node)) {
            return;
        }

        Node& load_node = nodes_.at(mode.load_node);
        load_node.pressure_bar_g = press_settings_.target_pressure_bar_g;
        load_node.has_pressure_boundary = true;
        update_node_head_from_pressure(load_node);
    }

    void apply_pressure_hold_state(const Mode& mode) {
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
        node.pressure_bar_abs = node.pressure_bar_g + kAtmosphericPressureBar;
        node.head_m = node.elevation_m
            + bar_to_pa(node.pressure_bar_g) / (fluid_.density_kg_m3 * fluid_.gravity_m_s2);
    }

    void update_node_from_head(const std::string& node_id, double head_m) {
        Node& node = nodes_.at(node_id);
        node.head_m = head_m;
        node.pressure_bar_g = pressure_bar_from_head(head_m - node.elevation_m);
        node.pressure_bar_abs = node.pressure_bar_g + kAtmosphericPressureBar;
    }

    double solve_flow_rate(const Mode& mode) const {
        double q_low = lpm_to_m3_s(solver_settings_.flow_search_min_L_min);
        double q_high = lpm_to_m3_s(solver_settings_.flow_search_max_L_min);
        double f_low = residual(mode, q_low);
        double f_high = residual(mode, q_high);

        if (f_low * f_high > 0.0) {
            double best_q = std::abs(f_low) <= std::abs(f_high) ? q_low : q_high;
            if(!quiet_) std::cout << "--- Solver warning ---\n";
            if(!quiet_) std::cout << "No residual sign change in "
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
                if(!quiet_) std::cout << "--- Convergence achieved in " << iter + 1 << " iterations ---\n";
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

        if(!quiet_) std::cout << "--- Bisection iteration limit reached ---\n";
        return q_mid;
    }

    double residual(const Mode& mode, double q_m3_s) const {
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
            recompute_cell_flow(item.second, q_m3_s);
        }
    }

    // leak 분기에서 리턴경로만 별도 유량(q_return)으로 다시 계산할 때 재사용.
    // active/경로 마킹은 건드리지 않고, 이미 update_cell_results()로 활성화된 셀의
    // 유량 파생값(velocity/loss/flow_rate_m3_s)만 다른 유량값으로 덮어쓴다.
    void reapply_flow_to_path(const std::vector<std::string>& path, double q_m3_s) {
        for (const auto& cell_id : path) {
            auto it = cells_.find(cell_id);
            if (it != cells_.end() && it->second.active) {
                recompute_cell_flow(it->second, q_m3_s);
            }
        }
    }

    // 셀 하나의 유량 파생값(velocity/Re/손실/펌프양정 등)을 주어진 q_m3_s로 계산.
    // update_cell_results()의 원래 내부 루프 바디를 그대로 추출한 것 — 동작 변경 없음.
    void recompute_cell_flow(Cell& cell, double q_m3_s) {
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
            return;
        }

        if (cell.type == "pump") {
            cell.pump_head_m = pump_head(cell, active_q);
            cell.pump_delta_pressure_bar = pressure_bar_from_head(cell.pump_head_m);
            return;
        }

        if (cell.diameter_m <= 0.0) {
            return;
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

    void propagate_supply_path(const Mode& mode, double q_m3_s) {
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

    void calculate_relief_branch_flow() {
#if 0
        Cell* relief = nullptr;
        for (auto& item : cells_) {
            if (item.second.line_type == "relief_valve") {
                relief = &item.second;
                break;
            }
        }
        if (!relief || relief->set_pressure_bar_g <= 0.0 || relief->kv_max_m3_h <= 0.0
            || !nodes_.contains(relief->from_node)) {
            return;
        }

        const double source_pressure_bar_g = nodes_.at(relief->from_node).pressure_bar_g;
        const double overpressure_bar = source_pressure_bar_g - relief->set_pressure_bar_g;
        if (overpressure_bar <= 0.0) {
            return;
        }

        // 릴리프는 설정압을 초과할 때만 열립니다. Kv 식으로 설정압을 초과한 압력에
        // 해당하는 유량을 계산하므로, 릴리프 설정값을 그대로 모델 입력에 넣지 않아도
        // 압력계·유량계에서 관측 가능한 물리 결과를 얻을 수 있습니다.
        const double specific_gravity = fluid_.density_kg_m3 / 1000.0;
        relief_flow_m3_s_ = relief->kv_max_m3_h
            * std::sqrt(overpressure_bar / specific_gravity) / 3600.0;

        // 수치상 펌프가 공급하는 총 유량보다 큰 릴리프 유량은 허용하지 않습니다.
        relief_flow_m3_s_ = std::min(relief_flow_m3_s_, std::max(0.0, last_q_m3_s_));
        if (relief_flow_m3_s_ <= 0.0) {
            return;
        }

        // CSV의 속도·Kv 결과도 실제로 릴리프 유로가 열렸음을 표현하도록 갱신합니다.
        relief->opening_percent = 100.0;
        relief->active = true;
        relief->active_from_node = relief->from_node;
        relief->active_to_node = relief->to_node;
        relief->flow_direction = "forward";
        recompute_cell_flow(*relief, relief_flow_m3_s_);

        for (auto& item : cells_) {
            Cell& return_pipe = item.second;
            if (return_pipe.line_type != "relief" || return_pipe.from_node != relief->to_node) {
                continue;
            }
            return_pipe.active = true;
            return_pipe.active_from_node = return_pipe.from_node;
            return_pipe.active_to_node = return_pipe.to_node;
            return_pipe.flow_direction = "forward";
            recompute_cell_flow(return_pipe, relief_flow_m3_s_);
            break;
        }
#endif
    }

    void mark_path_directions(const std::vector<std::string>& path, const std::string& start_node) {
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
        if(!quiet_) std::cout << std::fixed << std::setprecision(4);
        if(!quiet_) std::cout << "Calculated flow: " << m3_s_to_lpm(q_m3_s)
                  << " L/min (" << q_m3_s * 3600.0 << " m3/h)\n";
        if(!quiet_) std::cout << "Temperature-corrected viscosity: " << fluid_.viscosity_pa_s
                  << " Pa.s at " << fluid_.temperature_c << " C"
                  << " (reference " << fluid_.reference_viscosity_pa_s
                  << " Pa.s at " << fluid_.reference_temperature_c << " C)\n";
        if(!quiet_) std::cout << "Supply path: " << join_path(mode.supply_path) << '\n';
        if(!quiet_) std::cout << "Return path : " << join_path(mode.return_path) << '\n';

        if(!quiet_) std::cout << "\n--- Node Results ---\n";
        std::vector<std::string> node_ids;
        node_ids.reserve(nodes_.size());
        for (const auto& item : nodes_) {
            node_ids.push_back(item.first);
        }
        std::sort(node_ids.begin(), node_ids.end());

        for (const auto& id : node_ids) {
            const Node& n = nodes_.at(id);
            if(!quiet_) std::cout << n.id
                      << " [" << n.type << "]"
                      << " elev=" << n.elevation_m << " m"
                      << ", head=" << n.head_m << " m"
                      << ", P=" << n.pressure_bar_g << " bar(g)"
                      << ", P_abs=" << n.pressure_bar_abs << " bar(abs)"
                      << '\n';
        }

        if(!quiet_) std::cout << "\n--- Cell Results ---\n";
        for (const auto& id : cell_order_) {
            const Cell& c = cells_.at(id);
            if(!quiet_) std::cout << c.id
                      << " [" << c.type << "/" << c.line_type << "]"
                      << " active=" << (c.active ? "yes" : "no")
                      << ", direction=" << c.flow_direction;
            if (c.active) {
                if(!quiet_) std::cout << "(" << c.active_from_node << "->" << c.active_to_node << ")";
            }
            if(!quiet_) std::cout
                      << ", flow=" << m3_s_to_lpm(c.flow_rate_m3_s) << " L/min";

            if (c.type == "pump") {
                if(!quiet_) std::cout << ", pump_head=" << c.pump_head_m << " m"
                          << ", deltaP=" << c.pump_delta_pressure_bar << " bar"
                          << ", rpm=" << c.rpm
                          << ", rated_rpm=" << c.rated_rpm;
            } else {
                if(!quiet_) std::cout << ", velocity=" << c.velocity_m_s << " m/s"
                          << ", Re=" << c.reynolds_number
                          << ", regime=" << c.flow_regime
                          << ", friction_loss=" << c.friction_head_loss_m << " m"
                          << ", local_loss=" << c.local_head_loss_m << " m"
                          << ", total_loss=" << c.total_head_loss_m << " m"
                          << ", pressure_loss=" << c.pressure_loss_bar << " bar";
                if (c.type == "valve" && c.kv_max_m3_h > 0.0) {
                    if(!quiet_) std::cout << ", Kv_max=" << c.kv_max_m3_h << " m3/h"
                              << ", Kv_effective=" << c.kv_effective_m3_h << " m3/h"
                              << ", opening=" << c.opening_percent << " %";
                }
            }

            if(!quiet_) std::cout << ", dz=" << c.elevation_change_m << " m"
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

        if(!quiet_) std::cout << "\n--- Suction Check ---\n";
        if(!quiet_) std::cout << "Pump inlet: " << inlet.id
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

        if(!quiet_) std::cout << "\n--- Relief Check ---\n";
        if(!quiet_) std::cout << "P-port pressure=" << p_port.pressure_bar_g
                  << " bar(g), relief set=" << relief->set_pressure_bar_g
                  << " bar(g), relief_open=" << (open ? "yes" : "no") << '\n';
    }
};

static std::vector<std::string> splitCsv(const std::string& line){
    std::vector<std::string> out; std::string cur; bool q=false;
    for(char c: line){ if(c=='"'){q=!q;} else if(c==','&&!q){out.push_back(cur);cur.clear();} else cur+=c; }
    out.push_back(cur); return out;
}
static void setCell(json& root,const std::string& id,const std::string& key,double val){
    for(auto& c: root["cells"]) if(c.value("id","")==id){ c[key]=val; return; }
}
static void mulCell(json& root,const std::string& id,const std::string& key,double f){
    for(auto& c: root["cells"]) if(c.value("id","")==id){ double base=c.value(key,0.0); c[key]=base*f; return; }
}
static double vnum(const std::vector<std::string>& r,const std::unordered_map<std::string,int>& idx,const std::string& k,double def=0.0){
    auto it=idx.find(k); if(it==idx.end()||it->second>=(int)r.size()||r[it->second].empty()) return def;
    try{ return std::stod(r[it->second]); }catch(...){ return def; }
}
static std::string vstr(const std::vector<std::string>& r,const std::unordered_map<std::string,int>& idx,const std::string& k,const std::string& def=""){
    auto it=idx.find(k); if(it==idx.end()||it->second>=(int)r.size()) return def; return r[it->second];
}

int main(int argc,char** argv){
    if(argc<3){ std::cerr<<"usage: gen <network.json> <scenarios.csv>\n"; return 1; }
    std::ifstream nf(argv[1]); if(!nf){ std::cerr<<"cannot open network\n"; return 1; }
    json baseline; nf>>baseline;
    std::ifstream sf(argv[2]); if(!sf){ std::cerr<<"cannot open scenarios\n"; return 1; }
    std::string line; std::getline(sf,line);
    if(!line.empty()&&line.back()=='\r') line.pop_back();
    auto hdr=splitCsv(line); std::unordered_map<std::string,int> idx; for(int i=0;i<(int)hdr.size();++i) idx[hdr[i]]=i;

    std::vector<std::string> pipes={"C_SUCTION","C_PRESSURE","C_A_LINE","C_B_LINE","C_RETURN"};
    std::vector<std::string> vels={"C_SUCTION","C_PRESSURE","C_A_LINE","C_B_LINE","C_RETURN","V_RELIEF","C_RELIEF"};
    std::vector<std::string> valves={"V_DIR_PA","V_DIR_PB","V_DIR_AT","V_DIR_BT","V_RELIEF"};

    std::ostringstream ob; ob<<std::fixed<<std::setprecision(5);
    ob<<"row_id,active_mode,calculated_flow_rate_L_min,calculated_flow_rate_m3_h,hydraulic_power_kW,pump_head_m,pump_delta_pressure_bar,load_pressure_bar_g,target_pressure_error_bar,N_PUMP_IN_p,N_PUMP_OUT_p,N_VALVE_P_p,N_VALVE_A_p,N_VALVE_B_p,N_VALVE_T_p,N_CYL_CAP_p,N_CYL_ROD_p";
    for(auto&v:vels) ob<<",vel_"<<v; ob<<",max_active_velocity";
    for(auto&p:pipes) ob<<",loss_"<<p;
    for(auto&v:valves) ob<<",Kv_"<<v;
    ob<<",solver_warning,contains_inf_or_nan,status\n";

    while(std::getline(sf,line)){
        if(!line.empty()&&line.back()=='\r') line.pop_back();
        if(line.empty()) continue;
        auto r=splitCsv(line);
        std::string row_id=vstr(r,idx,"row_id");
        std::string mode=vstr(r,idx,"mode","downstroke");
        double tgt=vnum(r,idx,"target_pressure",0);
        json root=baseline;
        root["active_mode"]=mode;
        root["press"]["target_pressure_bar_g"]=tgt;
        root["fluid"]["temperature_c"]=vnum(r,idx,"temperature_c",40);
        setCell(root,"PUMP_01","rpm",vnum(r,idx,"pump_rpm",1771));
        setCell(root,"V_DIR_PA","opening_percent",vnum(r,idx,"v_pa",0));
        setCell(root,"V_DIR_PB","opening_percent",vnum(r,idx,"v_pb",0));
        setCell(root,"V_DIR_AT","opening_percent",vnum(r,idx,"v_at",0));
        setCell(root,"V_DIR_BT","opening_percent",vnum(r,idx,"v_bt",0));
        setCell(root,"V_RELIEF","opening_percent",vnum(r,idx,"v_relief_open",0));
        setCell(root,"V_RELIEF","set_pressure_bar_g",vnum(r,idx,"v_relief_set",250));
        double phs=vnum(r,idx,"pump_head_scale",1.0); if(phs!=1.0) mulCell(root,"PUMP_01","pump_head_shutoff_m",phs);
        std::string lc=vstr(r,idx,"loss_cell"); double lm=vnum(r,idx,"loss_k_mult",1.0), rm=vnum(r,idx,"rough_mult",1.0);
        if(!lc.empty()&&lm!=1.0) mulCell(root,lc,"local_loss_k",lm);
        if(!lc.empty()&&rm!=1.0) mulCell(root,lc,"roughness_m",rm);

        std::string warn="no",nan="no",status="ok";
        double flow=0,m3h=0,power=0,phead=0,pdelta=0,loadp=0,terr=0;
        std::vector<double> vv(vels.size(),0.0),ll(pipes.size(),0.0),kk(valves.size(),0.0);
        double maxv=0; double nP[8]={0,0,0,0,0,0,0,0};
        try{
            HydraulicTwinSolver s; s.quiet_=true;
            s.leak_K_=vnum(r,idx,"leak_K",0); s.leak_from_=vstr(r,idx,"leak_from"); s.leak_to_=vstr(r,idx,"leak_to"); s.load_cap_=vnum(r,idx,"load_cap",1e9);
            s.load_from_json(root);
            s.solve();
            double q=s.lastQ(); flow=q*60000.0; m3h=q*3600.0;
            const Cell* pc=s.cellPtr("PUMP_01");
            if(pc){ phead=pc->pump_head_m; pdelta=pc->pump_delta_pressure_bar; }
            power=pdelta*1e5*q/1000.0;
            std::string ln=s.activeMode().load_node; loadp=s.nodeP(ln); terr=std::fabs(tgt-loadp);
            const char* nn[8]={"N_PUMP_IN","N_PUMP_OUT","N_VALVE_P","N_VALVE_A","N_VALVE_B","N_VALVE_T","N_CYL_CAP","N_CYL_ROD"};
            for(int i=0;i<8;++i) nP[i]=s.nodeP(nn[i]);
            for(size_t i=0;i<vels.size();++i){ const Cell* c=s.cellPtr(vels[i]); if(c) vv[i]=c->velocity_m_s; }
            for(size_t i=0;i<pipes.size();++i){ const Cell* c=s.cellPtr(pipes[i]); if(c) ll[i]=c->pressure_loss_bar; }
            for(size_t i=0;i<valves.size();++i){ const Cell* c=s.cellPtr(valves[i]); if(c) kk[i]=c->kv_effective_m3_h; }
            maxv=s.maxActiveVelocity();
            auto bad=[&](double x){ return std::isnan(x)||std::isinf(x); };
            if(bad(flow)||bad(loadp)||bad(power)) nan="yes";
            for(double x:vv) if(bad(x)) nan="yes";
        }catch(const std::exception& ex){ status="error"; warn="yes"; }
        auto fx=[&](double x){ if(std::isnan(x)||std::isinf(x)) return std::string("0"); std::ostringstream o;o<<std::fixed<<std::setprecision(5)<<x; return o.str(); };
        ob<<row_id<<","<<mode<<","<<fx(flow)<<","<<fx(m3h)<<","<<fx(power)<<","<<fx(phead)<<","<<fx(pdelta)<<","<<fx(loadp)<<","<<fx(terr);
        for(int i=0;i<8;++i) ob<<","<<fx(nP[i]);
        for(double x:vv) ob<<","<<fx(x); ob<<","<<fx(maxv);
        for(double x:ll) ob<<","<<fx(x);
        for(double x:kk) ob<<","<<fx(x);
        ob<<","<<warn<<","<<nan<<","<<status<<"\n";
    }
    std::cout<<ob.str();
    return 0;
}

