import math

from environment import Environment, State, Action
from util import register
from encoding import CharEncoding

import torch
from torch import nn


class QFunction(nn.Module):
    """A Q-Function estimates the total expected reward of taking a certain
       action given that the agent is at a certain state. This module
       batches the computation and evaluates a set of actions given a state."""

    subtypes: dict = {}

    def forward(self, state: list[State], actions: list[Action]):
        raise NotImplementedError()

    def get_aggregation_transform(self):
        '''Returns a function that needs to be applied to the outputs of this QFunction
        so that its return values can be combined with a simple sum during beam search.'''
        # The default is for QFunctions to return probabilities, so log works.
        return math.log

    def rollout(self,
                environment: Environment,
                state: State,
                max_steps: int,
                beam_size: int = 1,
                debug: bool = False) -> tuple[bool, list[list[State]]]:
        """Runs beam search using the Q value until either
        max_steps have been made or reached a terminal state."""
        beam = [state]
        history = [beam]
        seen = set([state])
        success = False
        t = self.get_aggregation_transform()

        for i in range(max_steps):
            if debug:
                print(f'Beam #{i}: {beam}')

            if not beam:
                break

            rewards, s_actions = zip(*environment.step(beam))
            actions = [a for s_a in s_actions for a in s_a]

            if max(rewards):
                success = True
                break

            if len(actions) == 0:
                success = False
                break

            with torch.no_grad():
                q_values = self(actions).tolist()

            for a, v in zip(actions, q_values):
                a.next_state.value = a.state.value + t(v)

            ns = list(set([a.next_state for a in actions]) - seen)
            ns.sort(key=lambda s: s.value, reverse=True)

            if debug:
                print(f'Candidates: {[(s, s.value) for s in ns]}')

            beam = ns[:beam_size]
            history.append(ns)
            seen.update(ns)

        return success, history

    def recover_solutions(self, rollout_history: list[list[State]]) -> list[list[State]]:
        '''Reconstructs the solutions (lists of states) from the history of a successful rollout.'''

        solution_states = [s for s in rollout_history[-1] if s.value > 0]
        solutions = []

        for final_state in solution_states:
            s = final_state
            solution = [s]
            while s.parent_action is not None:
                s = s.parent_action.state
                solution.append(s)
            solutions.append(list(reversed(solution)))

        return solutions

    @staticmethod
    def new(config, device):
        return QFunction.subtypes[config['type']](config, device)

    def name(self):
        raise NotImplementedError()


@register(QFunction)
class DRRN(QFunction):
    def __init__(self, config, device):
        super().__init__()

        char_emb_dim = config.get('char_emb_dim', 128)
        self.hidden_dim = hidden_dim = config.get('hidden_dim', 256)
        self.lstm_layers = config.get('lstm_layers', 2)

        self.state_vocab = CharEncoding({'embedding_dim': char_emb_dim})
        self.action_vocab = CharEncoding({'embedding_dim': char_emb_dim})
        self.state_encoder = nn.LSTM(char_emb_dim, hidden_dim,
                                     self.lstm_layers, bidirectional=True)
        self.action_encoder = nn.LSTM(char_emb_dim, hidden_dim,
                                      self.lstm_layers, bidirectional=True)

        # Knob: whether to use the action description or the next state.
        # Options: 'state' or 'action'.
        self.action_label_type = config.get('action_label_type', 'action')

        self.to(device)

    def to(self, device):
        QFunction.to(self, device)
        self.device = device
        self.state_vocab.to(device)
        self.state_vocab.device = device
        self.action_vocab.to(device)
        self.action_vocab.device = device

    def forward(self, actions):
        state_embedding = self.embed_states([a.state for a in actions])
        action_embedding = self.embed_actions(actions)
        q_values = (action_embedding * state_embedding).sum(dim=1).sigmoid()
        return q_values

    def embed_states(self, states):
        N, H = len(states), self.hidden_dim
        states = [s.facts[-1] for s in states]
        state_seq, _ = self.state_vocab.embed_batch(states, self.device)
        state_seq = state_seq.transpose(0, 1)
        _, (state_hn, state_cn) = self.state_encoder(state_seq)
        state_embedding = (state_hn
                           .view(self.lstm_layers, 2, N, self.hidden_dim)[-1]
                           .permute((1, 2, 0)).reshape(N, 2*H))
        return state_embedding

    def embed_actions(self, actions):
        if self.action_label_type == 'action':
            actions = [a.action for a in actions]
        else:
            actions = [a.next_state.facts[-1] for a in actions]

        N, H = len(actions), self.hidden_dim
        actions_seq, _ = self.action_vocab.embed_batch(actions, self.device)
        actions_seq = actions_seq.transpose(0, 1)
        _, (actions_hn, actions_cn) = self.state_encoder(actions_seq)
        actions_embedding = (actions_hn
                             .view(self.lstm_layers, 2, N, self.hidden_dim)[-1]
                             .permute((1, 2, 0)).reshape((N, 2*H)))
        return actions_embedding

    def name(self):
        return 'DRRN'


# A simple architecture that just estimates the value of the next state.
@register(QFunction)
class StateRNNValueFn(QFunction):
    def __init__(self, config, device):
        super().__init__()

        char_emb_dim = config.get('char_emb_dim', 128)
        self.hidden_dim = hidden_dim = config.get('hidden_dim', 256)
        self.lstm_layers = config.get('lstm_layers', 2)

        self.vocab = CharEncoding({'embedding_dim': char_emb_dim})
        self.encoder = nn.LSTM(char_emb_dim, hidden_dim,
                               self.lstm_layers, bidirectional=True)
        self.output = nn.Linear(2*hidden_dim, 1)
        self.to(device)

    def to(self, device):
        QFunction.to(self, device)
        self.device = device
        self.vocab.device = device
        self.vocab.to(device)

    def forward(self, actions):
        state_embedding = self.embed_states([a.next_state for a in actions])
        q_values = self.output(state_embedding).sigmoid().squeeze(1)
        return q_values

    def embed_states(self, states):
        N, H = len(states), self.hidden_dim
        states = [s.facts[-1] for s in states]
        state_seq, _ = self.vocab.embed_batch(states, self.device)
        state_seq = state_seq.transpose(0, 1)
        _, (state_hn, state_cn) = self.encoder(state_seq)
        state_embedding = (state_hn
                           .view(self.lstm_layers, 2, N, self.hidden_dim)[-1]
                           .permute((1, 2, 0)).reshape(N, 2*H))
        return state_embedding

    def name(self):
        return 'StateRNNValueFn'


# A simple architecture that combines the current and next state embeddings with
# a bilinear transformation.
@register(QFunction)
class Bilinear(QFunction):
    def __init__(self, config, device):
        super().__init__()

        char_emb_dim = config.get('char_emb_dim', 128)
        self.hidden_dim = hidden_dim = config.get('hidden_dim', 256)
        self.lstm_layers = config.get('lstm_layers', 2)

        self.vocab = CharEncoding({'embedding_dim': char_emb_dim})
        self.encoder = nn.LSTM(char_emb_dim, hidden_dim,
                               self.lstm_layers, bidirectional=True)
        self.bilinear_comb = nn.Linear(2*hidden_dim, 2*hidden_dim)
        if config.get('mlp', False):
            self.mlp = True
            self.emb_mlp1 = nn.Linear(2*hidden_dim, 2*hidden_dim)
            self.emb_mlp2 = nn.Linear(2*hidden_dim, 2*hidden_dim)

        self.to(device)
        self.device = device

    def to(self, device):
        QFunction.to(self, device)
        self.device = device
        self.vocab.to(device)
        self.vocab.device = device


    def forward(self, actions):
        current_state_embedding = self.embed_states([a.state for a in actions])
        next_state_embedding = self.embed_states([a.next_state for a in actions])
        q_values = (self.bilinear_comb(current_state_embedding) * next_state_embedding)
        return q_values.sum(dim=1)

    def embed_states(self, states):
        N, H = len(states), self.hidden_dim
        states = [s.facts[-1] for s in states]
        state_seq, _ = self.vocab.embed_batch(states, self.device)
        state_seq = state_seq.transpose(0, 1)
        _, (state_hn, state_cn) = self.encoder(state_seq)
        state_embedding = (state_hn
                           .view(self.lstm_layers, 2, N, self.hidden_dim)[-1]
                           .permute((1, 2, 0)).reshape(N, 2*H))
        if getattr(self, 'mlp', False):
            state_embedding = self.emb_mlp1(state_embedding).relu()
            state_embedding = self.emb_mlp2(state_embedding)

        return state_embedding

    def name(self):
        return 'Bilinear'

    def get_aggregation_transform(self):
        return lambda x: x


class LearnerValueFunctionAdapter(QFunction):
    '''Adapter for the legacy LearnerValueFunction class to be used as a QFunction.'''

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, actions):
        s = [a.state.facts[-1] for a in actions]
        a = [a.action for a in actions]
        return self.model.forward(s, a)

    def __call__(self, actions):
        return self.forward(actions)

    def embed_states(self, states):
        s = [s.facts[-1] for s in states]
        return self.model.embed_state(s)

    def embed_actions(self, actions):
        return self.model.embed_action([a.action for a in actions])


class RandomQFunction(QFunction):
    def __init__(self, device=None):
        super().__init__()
        self.device = device

    def forward(self, actions):
        return torch.rand(len(actions)).to(device=self.device)


class InverseLength(QFunction):
    def __init__(self, device=None):
        super().__init__()
        self.device = device

    def forward(self, actions):
        return torch.tensor([1 / len(a.next_state.facts[-1]) for a in actions]).to(device=self.device)


class RubiksGreedyHeuristic(QFunction):
    'Simple bootstrap heuristic for the Rubik\'s cube that counts how many stickers are correct.'
    def __init__(self, device=None):
        super().__init__()
        self.device = device
        self.target = torch.tensor([0]*9 + [1]*9 + [2]*9 + [3]*9 + [4]*9 + [5]*9)

    def forward(self, actions):
        q = []

        for a in actions:
            digits = torch.tensor([int(d) for d in a.next_state.facts[-1] if d.isdigit()])
            q.append((digits == self.target).float().mean().item())

        return torch.tensor(q, device=self.device)
