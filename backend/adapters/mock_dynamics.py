"""Deterministic 3D point-mass dynamics for the Mock adapter.

The integration order follows the Multi-Agent Particle Environment (MPE):
apply damping, add action force over ``dt``, clamp speed, then integrate
position.  AeroWeaver uses three NED axes and a velocity controller on top of
that integrator so flight skills can submit targets instead of teleporting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


Vector3 = tuple[float, float, float]


def _vector(values: Iterable[float]) -> Vector3:
    items = [float(value) for value in values]
    if len(items) != 3:
        raise ValueError("Expected a three-dimensional vector")
    return items[0], items[1], items[2]


def _norm(values: Iterable[float]) -> float:
    x, y, z = _vector(values)
    return math.sqrt(x * x + y * y + z * z)


def _limit(values: Iterable[float], maximum: float) -> Vector3:
    vector = _vector(values)
    magnitude = _norm(vector)
    if maximum <= 0.0 or magnitude <= maximum or magnitude <= 1e-12:
        return vector
    scale = maximum / magnitude
    return tuple(value * scale for value in vector)


@dataclass(frozen=True)
class DynamicsStep:
    position: Vector3
    velocity: Vector3
    arrived: bool = False


class PointMassDynamics:
    """MPE-style point-mass integration with a bounded velocity controller."""

    def __init__(
        self,
        *,
        dt: float = 0.05,
        damping: float = 0.35,
        mass: float = 1.0,
        max_acceleration: float = 8.0,
        response_time: float = 0.35,
        arrival_tolerance: float = 0.08,
    ):
        self.dt = max(0.005, float(dt))
        self.damping = max(0.0, float(damping))
        self.mass = max(1e-6, float(mass))
        self.max_acceleration = max(0.1, float(max_acceleration))
        self.response_time = max(self.dt, float(response_time))
        self.arrival_tolerance = max(0.001, float(arrival_tolerance))

    def integrate(
        self,
        position: Iterable[float],
        velocity: Iterable[float],
        action_force: Iterable[float],
        max_speed: float,
    ) -> DynamicsStep:
        """Advance one fixed step using damping, force, speed cap and position."""
        pos = _vector(position)
        vel = _vector(velocity)
        force = _vector(action_force)
        damping_factor = max(0.0, 1.0 - self.damping * self.dt)
        damped = tuple(value * damping_factor for value in vel)
        accelerated = tuple(
            damped[axis] + (force[axis] / self.mass) * self.dt
            for axis in range(3)
        )
        next_velocity = _limit(accelerated, max(0.0, float(max_speed)))
        next_position = tuple(
            pos[axis] + next_velocity[axis] * self.dt
            for axis in range(3)
        )
        return DynamicsStep(next_position, next_velocity)

    def velocity_step(
        self,
        position: Iterable[float],
        velocity: Iterable[float],
        desired_velocity: Iterable[float],
        max_speed: float,
    ) -> DynamicsStep:
        """Track a desired NED velocity through a bounded control force."""
        vel = _vector(velocity)
        desired = _limit(desired_velocity, max_speed)
        acceleration = tuple(
            (desired[axis] - vel[axis]) / self.response_time
            for axis in range(3)
        )
        acceleration = _limit(acceleration, self.max_acceleration)
        force = tuple(value * self.mass for value in acceleration)
        return self.integrate(position, vel, force, max_speed)

    def target_step(
        self,
        position: Iterable[float],
        velocity: Iterable[float],
        target: Iterable[float],
        max_speed: float,
    ) -> DynamicsStep:
        """Advance toward a target with acceleration-limited braking."""
        pos = _vector(position)
        vel = _vector(velocity)
        goal = _vector(target)
        delta = tuple(goal[axis] - pos[axis] for axis in range(3))
        distance = _norm(delta)
        if distance <= self.arrival_tolerance:
            return DynamicsStep(goal, (0.0, 0.0, 0.0), True)

        direction = tuple(value / distance for value in delta)
        braking_speed = math.sqrt(2.0 * self.max_acceleration * distance)
        desired_speed = min(max(0.1, float(max_speed)), braking_speed)
        desired_velocity = tuple(value * desired_speed for value in direction)
        step = self.velocity_step(pos, vel, desired_velocity, max_speed)

        remaining = tuple(goal[axis] - step.position[axis] for axis in range(3))
        crossed_target = sum(
            delta[axis] * remaining[axis]
            for axis in range(3)
        ) <= 0.0
        if crossed_target or _norm(remaining) <= self.arrival_tolerance:
            return DynamicsStep(goal, (0.0, 0.0, 0.0), True)
        return step


__all__ = ["DynamicsStep", "PointMassDynamics"]
