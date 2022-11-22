#
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# This file taken from
#     https://github.com/deepmind/scalable_agent/blob/
#         cd66d00914d56c8ba2f0615d9cdeefcb169a8d70/vtrace.py
# and modified.

# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Functions to compute V-trace off-policy actor critic targets.

For details and theory see:

"IMPALA: Scalable Distributed Deep-RL with
Importance Weighted Actor-Learner Architectures"
by Espeholt, Soyer, Munos et al.

See https://arxiv.org/abs/1802.01561 for the full paper.
"""

import collections

import torch
import torch.nn.functional as F


VTraceFromLogitsReturns = collections.namedtuple(
    "VTraceFromLogitsReturns",
    ["vs", "pg_advantages", "log_rhos", "behavior_action_log_probs", "target_action_log_probs",],
)

VTraceReturns = collections.namedtuple("VTraceReturns", "vs pg_advantages rhos clipped_rhos")


def action_log_probs(policy_logits, actions, mask):
    return -(
        F.nll_loss(
            F.log_softmax(torch.flatten(policy_logits, end_dim=-2), dim=-1),
            torch.flatten(actions),
            reduction="none",
        ).view_as(actions)
        * mask
    ).sum(-1)


def from_logits(
    behavior_policy_logits,
    target_policy_logits,
    action_mask,
    actions,
    discounts,
    rewards,
    values,
    bootstrap_value,
    clip_rho_threshold=1.0,
    clip_pg_rho_threshold=1.0,
):
    r"""V-trace for softmax policies.

    Calculates V-trace actor critic targets for softmax polices as described
    in "IMPALA: Scalable Distributed Deep-RL with Importance Weighted
    Actor-Learner Architectures" by Espeholt, Soyer, Munos et al.
    Target policy refers to the policy we are interested in improving and
    behaviour policy refers to the policy that generated the given rewards
    and actions.
    In the notation used throughout documentation and comments, T refers to
    the time dimension ranging from 0 to T-1. B refers to the batch size and
    NUM_ACTIONS refers to the number of actions.

    Args:
      behaviour_policy_logits: A float32 tensor of shape [T, B, NUM_ACTIONS] with
        un-normalized log-probabilities parametrizing the softmax behaviour
        policy.
      target_policy_logits: A float32 tensor of shape [T, B, NUM_ACTIONS] with
        un-normalized log-probabilities parametrizing the softmax target policy.
      actions: An int32 tensor of shape [T, B] of actions sampled from the
        behaviour policy.
      discounts: A float32 tensor of shape [T, B] with the discount encountered
        when following the behaviour policy.
      rewards: A float32 tensor of shape [T, B] with the rewards generated by
        following the behaviour policy.
      values: A float32 tensor of shape [T, B] with the value function estimates
        wrt. the target policy.
      bootstrap_value: A float32 of shape [B] with the value function estimate at
        time T.
      clip_rho_threshold: A scalar float32 tensor with the clipping threshold for
        importance weights (rho) when calculating the baseline targets (vs).
        rho^bar in the paper.
      clip_pg_rho_threshold: A scalar float32 tensor with the clipping threshold
        on rho_s in \rho_s \delta log \pi(a|x) (r + \gamma v_{s+1} - V(x_s)).
      name: The name scope that all V-trace operations will be created in.
    Returns:
      A `VTraceFromLogitsReturns` namedtuple with the following fields:
        vs: A float32 tensor of shape [T, B]. Can be used as target to train a
            baseline (V(x_t) - vs_t)^2.
        pg_advantages: A float 32 tensor of shape [T, B]. Can be used as an
          estimate of the advantage in the calculation of policy gradients.
        log_rhos: A float32 tensor of shape [T, B] containing the log importance
          sampling weights (log rhos).
        behaviour_action_log_probs: A float32 tensor of shape [T, B] containing
          behaviour policy action log probabilities (log \mu(a_t)).
        target_action_log_probs: A float32 tensor of shape [T, B] containing
          target policy action probabilities (log \pi(a_t)).
    """

    target_action_log_probs = action_log_probs(target_policy_logits, actions, action_mask)
    behavior_action_log_probs = action_log_probs(behavior_policy_logits, actions, action_mask)
    log_rhos = target_action_log_probs - behavior_action_log_probs
    vtrace_returns = from_importance_weights(
        log_rhos=log_rhos,
        discounts=discounts,
        rewards=rewards,
        values=values,
        bootstrap_value=bootstrap_value,
        clip_rho_threshold=clip_rho_threshold,
        clip_pg_rho_threshold=clip_pg_rho_threshold,
    )
    return VTraceFromLogitsReturns(
        log_rhos=log_rhos,
        behavior_action_log_probs=behavior_action_log_probs,
        target_action_log_probs=target_action_log_probs,
        **vtrace_returns._asdict(),
    )


@torch.no_grad()
def from_importance_weights(
    log_rhos,
    discounts,
    rewards,
    values,
    bootstrap_value,
    clip_rho_threshold=1.0,
    clip_pg_rho_threshold=1.0,
):
    r"""V-trace from log importance weights.

    Calculates V-trace actor critic targets as described in

    "IMPALA: Scalable Distributed Deep-RL with
    Importance Weighted Actor-Learner Architectures"
    by Espeholt, Soyer, Munos et al.

    In the notation used throughout documentation and comments, T refers to the
    time dimension ranging from 0 to T-1. B refers to the batch size and
    NUM_ACTIONS refers to the number of actions. This code also supports the
    case where all tensors have the same number of additional dimensions, e.g.,
    `rewards` is [T, B, C], `values` is [T, B, C], `bootstrap_value` is [B, C].

    Args:
      log_rhos: A float32 tensor of shape [T, B, NUM_ACTIONS] representing the log
        importance sampling weights, i.e.
        log(target_policy(a) / behaviour_policy(a)). V-trace performs operations
        on rhos in log-space for numerical stability.
      discounts: A float32 tensor of shape [T, B] with discounts encountered when
        following the behaviour policy.
      rewards: A float32 tensor of shape [T, B] containing rewards generated by
        following the behaviour policy.
      values: A float32 tensor of shape [T, B] with the value function estimates
        wrt. the target policy.
      bootstrap_value: A float32 of shape [B] with the value function estimate at
        time T.
      clip_rho_threshold: A scalar float32 tensor with the clipping threshold for
        importance weights (rho) when calculating the baseline targets (vs).
        rho^bar in the paper. If None, no clipping is applied.
      clip_pg_rho_threshold: A scalar float32 tensor with the clipping threshold
        on rho_s in \rho_s \delta log \pi(a|x) (r + \gamma v_{s+1} - V(x_s)). If
        None, no clipping is applied.
      name: The name scope that all V-trace operations will be created in.

    Returns:
      A VTraceReturns namedtuple (vs, pg_advantages) where:
        vs: A float32 tensor of shape [T, B]. Can be used as target to
          train a baseline (V(x_t) - vs_t)^2.
        pg_advantages: A float32 tensor of shape [T, B]. Can be used as the
          advantage in the calculation of policy gradients.
    """
    with torch.no_grad():
        rhos = torch.exp(log_rhos)
        if clip_rho_threshold is not None:
            clipped_rhos = torch.clamp(rhos, max=clip_rho_threshold)
        else:
            clipped_rhos = rhos

        cs = torch.clamp(rhos, max=1.0)
        # Append bootstrapped value to get [v1, ..., v_t+1]
        values_t_plus_1 = torch.cat([values[1:], torch.unsqueeze(bootstrap_value, 0)], dim=0)
        deltas = clipped_rhos * (rewards + discounts * values_t_plus_1 - values)

        acc = torch.zeros_like(bootstrap_value)
        result = []
        for t in range(discounts.shape[0] - 1, -1, -1):
            acc = deltas[t] + discounts[t] * cs[t] * acc
            result.append(acc)
        result.reverse()
        vs_minus_v_xs = torch.stack(result)

        # Add V(x_s) to get v_s.
        vs = torch.add(vs_minus_v_xs, values)

        # Advantage for policy gradient.
        vs_t_plus_1 = torch.cat([vs[1:], torch.unsqueeze(bootstrap_value, 0)], dim=0)
        if clip_pg_rho_threshold is not None:
            clipped_pg_rhos = torch.clamp(rhos, max=clip_pg_rho_threshold)
        else:
            clipped_pg_rhos = rhos
        pg_advantages = clipped_pg_rhos * (rewards + discounts * vs_t_plus_1 - values)

        # Make sure no gradients backpropagated through the returned values.
        return VTraceReturns(
            vs=vs, pg_advantages=pg_advantages, rhos=rhos, clipped_rhos=clipped_rhos
        )
