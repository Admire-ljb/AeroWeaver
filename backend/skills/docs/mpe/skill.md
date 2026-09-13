# Native MPE Skill Profile

Source: Farama MPE2 1.1.0 and PettingZoo 1.27.0. This profile is separate
from the 3D Mock/AirSim flight catalog. It is activated once per selected task,
then filtered by the participant's native role and action permissions.

## Representation and Execution

Each skill has a name, semantic description, parameter schema, a bound owner,
and a one-cycle native action executor. Motion skills take a local
`target_delta: [x,y]` and the participant's own `velocity: [vx,vy]`.
A fixed local PD controller maps these into a native discrete force action.
`avoid_collision` and `evade_pursuer` move away from the supplied local displacement.
`coast` applies zero force; native damping still applies. It is not a position reset.
Signal skills take an integer `symbol` within the native channel's alphabet.
Joint motion-and-signal skills encode `movement + 5 * symbol` as in native MPE2.

Only the calling participant's action is produced. All participants' actions
are applied together by the environment's Parallel API. The executor does not
advance physics separately per participant, rescale rewards, add a capture
radius, grant a stationary role movement, or create a supplementary peer channel.

## Task-Level Activation

| Scenario | Native Role | Active Skills |
| --- | --- | --- |
| simple_spread | searcher | cover_landmark, avoid_collision, coast |
| simple_tag | pursuer | pursue_target, avoid_collision, coast |
| simple_tag | evader | evade_pursuer, return_in_bounds, coast |
| simple_speaker_listener | speaker | signal_goal |
| simple_speaker_listener | listener | navigate_to_landmark, coast |
| simple_crypto | sender / receiver / eavesdropper | encode_message / decode_message / guess_message, respectively |
| simple_formation | formation_member | take_circle_slot, coast |
| simple_line | formation_member | take_line_slot, coast |
| simple_world_comm | leader_adversary | pursue_and_signal, coast_and_signal |
| simple_world_comm | adversary | pursue_target, follow_signal, coast |
| simple_world_comm | good_agent | seek_food, seek_cover, evade_pursuer, coast |
| collect_treasure | collector | collect_treasure, deliver_treasure, coast |
| collect_treasure | depositor | meet_collector, coast |
| simple_adversary | good_agent | approach_goal, approach_decoy, coast |
| simple_adversary | adversary | approach_inferred_goal, coast |

## Information and Reward Boundaries

Private communication: Alice receives the message and key, Bob the key and
public message, Eve only the public message. Their skills only emit symbols.
World communication: the leader is a mobile environment participant, not the
Commander. Native forest masking and leader messages remain unchanged.
Collection: depositors are mobile participants. Delivery matches treasure type.
Concealment: only the good agents' native observations contain the target.
Circular and line formation retain the native geometry and matching reward.
Tag uses native collision rewards, not the flight demo's 6-metre capture rule.

The UI world view is privileged visualization, never an input to a local policy.
The engineering selectors are deterministic hand-written policies or random
skill selection. Their scores are not LLM token logits and their measured
returns are not evidence of LLM-based AeroWeaver performance.

## Online Memory Pilot

Only completed episodes enter memory. Retrieval is within the same scenario
and team, with role sharing or an individual-only control. The advantage is
the matching skill's mean native return minus the retrieved neighborhood's
mean return. Missing matches receive zero correction. Model weights, skill
definitions, environment dynamics and native rewards remain unchanged.
