"""AeroWeaver skill package runtime extensions."""

from skills.online_skill_bootstrap import install as _install_online_skills

_install_online_skills()

from skills.swarm_recovery_patch import install as _install_swarm_recovery

_install_swarm_recovery()
