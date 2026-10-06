"""Bounded-dimension SciPy placement subproblems with conservative legalization.

Every local solve has at most 2 * block_size variables. NumPy arrays replace
per-evaluation board copying, and analytic Jacobians avoid board-sized finite
differences. Separation directions are fixed within a subproblem and reconsidered
between blocks. This is local optimization, not a global placement guarantee.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
from scipy.optimize import minimize
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from .board import collision_boxes


class _Problem:
    def __init__(self, board, constraints, refs, bounds):
        footprints = board["footprints"]
        self.lookup = {fp["reference"]: i for i, fp in enumerate(footprints)}
        self.positions = np.array([[fp["x"], fp["y"]] for fp in footprints], dtype=float)
        self.offsets = np.array([fp["bounds"] for fp in footprints]) - np.tile(self.positions, (1, 2))
        self.movable = np.array([self.lookup[ref] for ref in refs], dtype=int)
        self.limits = np.asarray(bounds, dtype=float).reshape(-1, 2, 2)
        self.limit_lookup = {int(index): i for i, index in enumerate(self.movable)}
        self.gap = constraints.get("board", {}).get("component_gap_mm", 0)
        pairs, first_offsets, second_offsets = [], [], []
        for a, b in combinations(range(len(footprints)), 2):
            for first, second in collision_boxes(footprints[a], footprints[b]):
                pairs.append((a, b))
                first_offsets.append(np.asarray(first) - np.tile(self.positions[a], 2))
                second_offsets.append(np.asarray(second) - np.tile(self.positions[b], 2))
        self.pairs = np.asarray(pairs, dtype=int).reshape(-1, 2)
        self.first_offsets = np.asarray(first_offsets, dtype=float).reshape(-1, 4)
        self.second_offsets = np.asarray(second_offsets, dtype=float).reshape(-1, 4)
        owners, offsets, nets = [], [], []
        net_lookup = {}
        for owner, fp in enumerate(footprints):
            for pad in fp["pads"]:
                if pad["net"]:
                    owners.append(owner)
                    offsets.append([pad["x"] - fp["x"], pad["y"] - fp["y"]])
                    nets.append(net_lookup.setdefault(pad["net"], len(net_lookup)))
        self.owners = np.asarray(owners, dtype=int)
        self.pad_offsets = np.asarray(offsets, dtype=float).reshape(-1, 2)
        self.nets = np.asarray(nets, dtype=int)
        self.weights = np.ones(len(net_lookup))
        for name in constraints.get("critical_nets", []):
            if name in net_lookup:
                self.weights[net_lookup[name]] = 2
        self.proximity = [(self.lookup[p["reference"]], self.lookup[p["target"]], p["max_distance_mm"])
                          for p in constraints.get("proximity", [])
                          if p["reference"] in self.lookup and p["target"] in self.lookup]

    def hpwl(self, positions, gradient=False):
        if not len(self.nets):
            return (0.0, np.zeros_like(positions)) if gradient else 0.0
        pads = positions[self.owners] + self.pad_offsets
        low = np.full((len(self.weights), 2), np.inf)
        high = np.full_like(low, -np.inf)
        np.minimum.at(low, self.nets, pads)
        np.maximum.at(high, self.nets, pads)
        value = float(((high - low).sum(axis=1) * self.weights).sum())
        if not gradient:
            return value
        grad = np.zeros_like(positions)
        for axis in range(2):
            is_low = pads[:, axis] == low[self.nets, axis]
            is_high = pads[:, axis] == high[self.nets, axis]
            low_count = np.bincount(self.nets, weights=is_low, minlength=len(self.weights))
            high_count = np.bincount(self.nets, weights=is_high, minlength=len(self.weights))
            contribution = (is_high / high_count[self.nets] - is_low / low_count[self.nets]) * self.weights[self.nets]
            np.add.at(grad[:, axis], self.owners, contribution)
        return value, grad

    def separations(self, positions, pairs=None):
        selection = slice(None) if pairs is None else pairs
        owners = self.pairs[selection]
        a = self.first_offsets[selection] + np.tile(positions[owners[:, 0]], (1, 2))
        b = self.second_offsets[selection] + np.tile(positions[owners[:, 1]], (1, 2))
        return np.column_stack((b[:, 0] - a[:, 2], a[:, 0] - b[:, 2],
                                b[:, 1] - a[:, 3], a[:, 1] - b[:, 3]))

    def quality(self, positions, pairs=None, movable=None):
        violations = [np.minimum(self.separations(positions, pairs).max(axis=1) - self.gap, 0)]
        indices = self.movable if movable is None else np.asarray(movable, dtype=int)
        limits = self.limits[[self.limit_lookup[int(i)] for i in indices]]
        chosen = positions[indices]
        violations.extend([np.minimum(chosen - limits[:, :, 0], 0).ravel(),
                           np.minimum(limits[:, :, 1] - chosen, 0).ravel()])
        active = set(map(int, indices))
        violations.append(np.array([min(radius - np.linalg.norm(positions[a] - positions[b]), 0)
                                    for a, b, radius in self.proximity if movable is None or a in active or b in active]))
        negative = np.concatenate(violations)
        negative[np.abs(negative) <= 1e-6] = 0
        return float(negative @ negative), self.hpwl(positions)

    def legalize(self, positions, rng):
        """Bounded constructive start; failure is retained for the final audit."""
        if self.quality(positions)[0] == 0:
            return positions, 0
        result = positions.copy()
        todo = set(map(int, self.movable))
        placed = set(range(len(positions))) - todo
        area = (self.offsets[:, 2] - self.offsets[:, 0]) * (self.offsets[:, 3] - self.offsets[:, 1])
        tie = rng.random(len(positions))
        unresolved = 0
        while todo:
            def priority(index):
                anchored = sum((a == index and b in placed) or (b == index and a in placed)
                               for a, b, _ in self.proximity)
                return -anchored, -area[index], tie[index], index

            index = min(todo, key=priority)
            limits = self.limits[self.limit_lookup[index]]
            preferred = np.clip(result[index], limits[:, 0], limits[:, 1])
            width, height = self.offsets[index, 2:] - self.offsets[index, :2] + self.gap
            axes = []
            for axis, step in enumerate((width, height)):
                lower, upper = limits[axis]
                count = min(80, max(2, int(math.ceil((upper - lower) / max(step, 0.1))) + 1))
                axes.append(np.unique(np.r_[np.linspace(lower, upper, count), preferred[axis]]))
            xx, yy = np.meshgrid(*axes)
            options = np.column_stack((xx.ravel(), yy.ravel()))
            extra = [preferred]
            neighbours = []
            for a, b, radius in self.proximity:
                neighbour = b if a == index else a if b == index else None
                if neighbour is not None and neighbour in placed:
                    neighbours.append((neighbour, radius))
                    extra.append(result[neighbour])
                    for angle in np.linspace(0, 2 * np.pi, 16, endpoint=False):
                        extra.append(result[neighbour] + radius * np.array([math.cos(angle), math.sin(angle)]))
            options = np.vstack((extra, options))
            valid = ((options >= limits[:, 0] - 1e-9) & (options <= limits[:, 1] + 1e-9)).all(axis=1)
            for neighbour, radius in neighbours:
                valid &= np.linalg.norm(options - result[neighbour], axis=1) <= radius + 1e-7
            options = options[valid]
            order = np.argsort(((options - preferred) ** 2).sum(axis=1), kind="stable")
            options = options[order]
            left, right = self.pairs.T
            selected = ((left == index) & np.isin(right, list(placed))) | ((right == index) & np.isin(left, list(placed)))
            owners = self.pairs[selected]
            first_offsets, second_offsets = self.first_offsets[selected], self.second_offsets[selected]
            first_positions, second_positions = result[owners[:, 0]], result[owners[:, 1]]
            found = False
            for start in range(0, len(options), 128):
                candidate = options[start:start + 128]
                first = np.where((owners[:, 0] == index)[None, :, None], candidate[:, None, :], first_positions[None, :, :])
                second = np.where((owners[:, 1] == index)[None, :, None], candidate[:, None, :], second_positions[None, :, :])
                a = first_offsets[None, :, :] + np.tile(first, (1, 1, 2))
                b = second_offsets[None, :, :] + np.tile(second, (1, 1, 2))
                dx = np.maximum(b[:, :, 0] - a[:, :, 2], a[:, :, 0] - b[:, :, 2])
                dy = np.maximum(b[:, :, 1] - a[:, :, 3], a[:, :, 1] - b[:, :, 3])
                legal = (np.maximum(dx, dy) >= self.gap - 1e-7).all(axis=1)
                if np.any(legal):
                    result[index] = candidate[np.flatnonzero(legal)[0]]
                    found = True
                    break
            if not found:
                result[index] = preferred
                unresolved += 1
            todo.remove(index)
            placed.add(index)
        return result, unresolved

    def solve(self, positions, block, iterations, objective_function=None, collision_axis=None):
        n = len(positions)
        block = np.asarray(block, dtype=int)
        local = np.full(n, -1, dtype=int)
        local[block] = np.arange(len(block))
        selected = np.flatnonzero((local[self.pairs[:, 0]] >= 0) | (local[self.pairs[:, 1]] >= 0))
        pairs = self.pairs[selected]
        sides = self.separations(positions, selected)
        directions = np.argmax(sides, axis=1)
        if collision_axis is not None:
            colliding = sides.max(axis=1) < self.gap - 1e-6
            axis_sides = sides[colliding, 2 * collision_axis:2 * collision_axis + 2]
            directions[colliding] = 2 * collision_axis + np.argmax(axis_sides, axis=1)
        matrix = np.zeros((len(pairs), 2 * len(block)))
        for row, ((a, b), direction) in enumerate(zip(pairs, directions)):
            axis, sign = int(direction // 2), 1 if direction % 2 == 0 else -1
            if local[a] >= 0:
                matrix[row, 2 * local[a] + axis] = -sign
            if local[b] >= 0:
                matrix[row, 2 * local[b] + axis] = sign
        initial = positions[block].ravel()
        constant = sides[np.arange(len(pairs)), directions] - self.gap - matrix @ initial
        prox = [(a, b, radius) for a, b, radius in self.proximity if local[a] >= 0 or local[b] >= 0]

        def expanded(vector):
            result = positions.copy()
            result[block] = vector.reshape(-1, 2)
            return result

        def objective(vector):
            function = self.hpwl if objective_function is None else objective_function
            value, gradient = function(expanded(vector), gradient=True)
            return value, gradient[block].ravel()

        def proximity_values(vector):
            result = expanded(vector)
            return np.array([radius - np.linalg.norm(result[a] - result[b]) for a, b, radius in prox])

        def proximity_jacobian(vector):
            result = expanded(vector)
            jac = np.zeros((len(prox), len(vector)))
            for row, (a, b, _) in enumerate(prox):
                delta = result[a] - result[b]
                delta /= max(float(np.linalg.norm(delta)), 1e-12)
                if local[a] >= 0:
                    jac[row, 2 * local[a]:2 * local[a] + 2] -= delta
                if local[b] >= 0:
                    jac[row, 2 * local[b]:2 * local[b] + 2] += delta
            return jac

        constraints = []
        if len(pairs):
            constraints.append({"type": "ineq", "fun": lambda v: matrix @ v + constant, "jac": lambda v: matrix})
        if prox:
            constraints.append({"type": "ineq", "fun": proximity_values, "jac": proximity_jacobian})
        limits = self.limits[[self.limit_lookup[int(i)] for i in block]].reshape(-1, 2)
        solved = minimize(objective, initial, jac=True, method="SLSQP", bounds=limits,
                          constraints=constraints, options={"maxiter": iterations, "ftol": 1e-7})
        return expanded(solved.x), solved, selected


def legalize_minimum_displacement(board, constraints, refs, bounds, options, candidate_index=0):
    """Repair violated neighborhoods without repacking otherwise legal components.

    Each SLSQP subproblem minimizes squared displacement from the input, not
    HPWL. Stalled groups can include nearby movable neighbors in later passes.
    Large groups are partitioned to respect block_size, so failure within the
    budget is possible and must remain visible in the independent final audit.
    """
    problem = _Problem(board, constraints, refs, bounds)
    positions = problem.positions.copy()
    movable = set(map(int, problem.movable))
    rng = np.random.default_rng(np.random.SeedSequence([options.seed, candidate_index]))
    summary = {"method": "scipy.optimize.minimize/SLSQP/minimum-displacement",
               "objective": "sum_squared_displacement_mm2", "iterations": 0, "evaluations": 0,
               "subproblems": 0, "successful_subproblems": 0, "accepted_blocks": 0,
               "passes_completed": 0, "max_subproblem_variables": 0, "analytic_jacobians": True}

    def displacement(points, gradient=False):
        delta = points - problem.positions
        value = float((delta * delta).sum())
        return (value, 2 * delta) if gradient else value

    for sweep in range(options.passes):
        if problem.quality(positions)[0] == 0:
            break
        separation = problem.separations(positions).max(axis=1)
        bad = problem.pairs[separation < problem.gap - 1e-6]
        proximity = [(a, b) for a, b, radius in problem.proximity
                     if np.linalg.norm(positions[a] - positions[b]) > radius + 1e-6]
        if proximity:
            bad = np.vstack((bad, np.asarray(proximity, dtype=int)))
        chosen = positions[problem.movable]
        outside = ((chosen < problem.limits[:, :, 0] - 1e-6) |
                   (chosen > problem.limits[:, :, 1] + 1e-6)).any(axis=1)
        involved = (set(map(int, bad.ravel())) | set(map(int, problem.movable[outside]))) & movable
        if not involved:
            break
        graph = coo_matrix((np.ones(2 * len(bad)),
                            (np.r_[bad[:, 0], bad[:, 1]], np.r_[bad[:, 1], bad[:, 0]])),
                           shape=(len(positions), len(positions))).tocsr()
        _, labels = connected_components(graph, directed=False)
        groups = [[i for i in sorted(involved) if labels[i] == label]
                  for label in sorted(set(labels[list(involved)]))]
        if candidate_index:
            rng.shuffle(groups)
        for group in groups:
            if sweep and len(group) < options.block_size:
                separation = problem.separations(positions).max(axis=1)
                nearby = {}
                for (a, b), distance in zip(problem.pairs, separation):
                    if a in group and b in movable and b not in group:
                        nearby[int(b)] = min(nearby.get(int(b), np.inf), distance)
                    if b in group and a in movable and a not in group:
                        nearby[int(a)] = min(nearby.get(int(a), np.inf), distance)
                group = group + [i for i in sorted(nearby, key=lambda i: (nearby[i], i))
                                 if nearby[i] < 0.5 * sweep][:options.block_size - len(group)]
            for offset in range(0, len(group), options.block_size):
                block = group[offset:offset + options.block_size]
                before = problem.quality(positions)[0]
                # Multiple holes of one package can demand contradictory nearest
                # separation directions. Retry each axis before enlarging a group.
                for axis in (None, 1, 0):
                    proposed, solved, _ = problem.solve(positions, block, options.max_iterations, displacement, axis)
                    summary["subproblems"] += 1
                    summary["successful_subproblems"] += int(solved.success)
                    summary["iterations"] += int(solved.get("nit", 0))
                    summary["evaluations"] += int(solved.get("nfev", 0))
                    summary["max_subproblem_variables"] = max(summary["max_subproblem_variables"], 2 * len(block))
                    if np.isfinite(proposed).all() and problem.quality(proposed)[0] < before - 1e-12:
                        positions = proposed
                        summary["accepted_blocks"] += 1
                        break
                    if solved.success:
                        break
        summary["passes_completed"] = sweep + 1
    summary["success"] = summary["successful_subproblems"] == summary["subproblems"]
    summary["remaining_constraint_loss_mm2"] = problem.quality(positions)[0]
    summary["objective_value_mm2"] = displacement(positions)
    summary["message"] = "Local displacement repair complete; no HPWL refinement; independent audit determines feasibility"
    return positions[problem.movable].ravel(), summary


def optimize_blocks(board, constraints, refs, bounds, options, candidate_index=0):
    problem = _Problem(board, constraints, refs, bounds)
    rng = np.random.default_rng(np.random.SeedSequence([options.seed, candidate_index]))
    positions, unresolved = problem.legalize(problem.positions, rng)
    if problem.quality(problem.positions) < problem.quality(positions):
        positions = problem.positions.copy()
    summary = {"method": "scipy.optimize.minimize/SLSQP/block-coordinate", "iterations": 0,
               "evaluations": 0, "subproblems": 0, "successful_subproblems": 0,
               "accepted_blocks": 0, "passes_completed": 0, "max_subproblem_variables": 0,
               "legalization_unresolved": unresolved, "analytic_jacobians": True}
    for sweep in range(options.passes):
        accepted_before = summary["accepted_blocks"]
        remaining = set(map(int, problem.movable))
        tree = cKDTree(positions[problem.movable])
        seeds = rng.permutation(problem.movable)
        for seed in seeds:
            if int(seed) not in remaining:
                continue
            _, neighbours = tree.query(positions[seed], k=len(problem.movable))
            block = [int(problem.movable[i]) for i in np.atleast_1d(neighbours) if int(problem.movable[i]) in remaining][:options.block_size]
            remaining.difference_update(block)
            proposed, result, pairs = problem.solve(positions, block, options.max_iterations)
            summary["subproblems"] += 1
            summary["successful_subproblems"] += int(result.success)
            summary["iterations"] += int(result.get("nit", 0))
            summary["evaluations"] += int(result.get("nfev", 0))
            summary["max_subproblem_variables"] = max(summary["max_subproblem_variables"], 2 * len(block))
            if np.isfinite(proposed).all():
                quality = problem.quality(positions, pairs, block)
                candidate_quality = problem.quality(proposed, pairs, block)
                if candidate_quality[0] < quality[0] - 1e-9 or (candidate_quality[0] <= quality[0] and candidate_quality[1] < quality[1] - 1e-7):
                    positions = proposed
                    summary["accepted_blocks"] += 1
        summary["passes_completed"] = sweep + 1
        if accepted_before == summary["accepted_blocks"]:
            break
    summary["success"] = summary["successful_subproblems"] == summary["subproblems"]
    summary["message"] = "Bounded local solves complete; candidate feasibility requires independent audit"
    return positions[problem.movable].ravel(), summary
