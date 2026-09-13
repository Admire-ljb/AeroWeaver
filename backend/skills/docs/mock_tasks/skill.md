# Mock Task Skills

These skills operate within the existing MockAdapter point-mass scene. They are
not native MPE actions and do not produce native MPE rewards.

## Activation and roles

A task activates the union of its roles' skills once. Each local agent then sees
only its role's subset and targets visible to that agent. Commander initializes
the mission and receives progress reports; it does not invoke flight skills.

| Task | Roles | Main skills |
| --- | --- | --- |
| Coverage | searchers | cover_landmark |
| Pursuit-evasion | pursuers, faster evader | pursue_target, follow_peer, evade_peer |
| Guided navigation | stationary speaker, mobile listener | signal_goal, follow_signal |
| Private communication | sender, receiver, eavesdropper | encode_message, decode_message, guess_message |
| Circular / line formation | formation members | take_formation_slot |
| World communication | mobile leader, pursuers, foragers | signal_target, pursue_target, seek_food, seek_cover |
| Collection-delivery | collectors, typed mobile deposits | collect_treasure, deliver_treasure, meet_collector |
| Goal concealment | informed agents, adversary | approach_goal, approach_decoy, infer_goal |

## Invocation

Invoke a skill from the agent's current `options`. Supply `target_id` for a
targeted skill or `symbol` for a symbolic communication skill. Motion remains
body-local and the local waypoint tracker continues between selector calls.
`hold_position` stops only this body. Teleportation is confined to initialization.

Speaker goal information is delivered through a directed message. Only the
sender and receiver have the private key; the eavesdropper sees ciphertext.

Private communication uses two-bit XOR symbols in 0..3. The sender transmits
`ciphertext = plaintext XOR private_key`. The receiver computes
`plaintext = ciphertext XOR private_key` and submits that reconstructed plaintext
as `decode_message(symbol)`. This executor records the submitted symbol; it does
not decrypt the argument again. Receivers and eavesdroppers wait with
`hold_position` until a ciphertext message is available. The eavesdropper receives
neither the key nor the plaintext. A completed three-round protocol is not itself
evidence of successful decoding or secrecy.
Private goals are absent from listener and adversary contexts. Leader is a mobile
participant, distinct from Commander. Treasure pickup requires proximity, one
free cargo slot, and delivery to the matching deposit type.

## Evaluation scope

The deterministic local selector is an engineering baseline, not an LLM or RL
result. Task diagnostics are recorded separately from reward. Native MPE reward
and dynamics equivalence are not claimed. The task forms follow the manuscript,
while movement, sensing ranges, boundaries and contact thresholds are Mock-specific.
