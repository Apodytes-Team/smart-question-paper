# Implementation of Reinforcement Learning agents that interact with the
# educational domain environment implemented in Racket.

import argparse
import collections
import copy
import datetime
import time
import itertools
import urllib
import requests
import random
import traceback
import sys
import pickle
import json
import torch
import math
import util
import wandb
import logging
import subprocess
from torch import nn
from torch.nn import functional as F
from torch.distributions.categorical import Categorical
import pytorch_lightning as pl

from domain_learner import CharEncoding, LearnerValueFunction, collate_concat

class State:
    def __init__(self, facts, goals, value, parent_action=None):
        self.facts = tuple(facts)
        self.goals = tuple(goals)
        self.value = value
        self.parent_action = parent_action

    def __hash__(self):
        return hash(self.facts)

    def __str__(self):
        return 'State({})'.format(self.facts[-1])

    def __repr__(self):
        return str(self)

SUCCESS_STATE = State(['success'], [], 1.0)

class Action:
    def __init__(self, state, action, next_state, reward, value=0.0):
        self.state = state
        self.action = action
        self.next_state = next_state
        self.reward = reward
        self.value = value

    def __str__(self):
        return 'Action({})'.format(self.action)

    def __repr__(self):
        return str(self)

class QFunction(nn.Module):
    """A Q-Function estimates the total expected reward of taking a certain
       action given that the agent is at a certain state. This module
       batches the computation and evaluates a set of actions given one state."""

    def forward(self, state, actions):
        raise NotImplemented()

    def rollout(self, environment, state, max_steps, beam_size=1, debug=False):
        """Runs beam search using the Q value until either
        max_steps have been made or reached a terminal state."""
        beam = [state]
        history = []
        success = False

        for i in range(max_steps):
            if debug:
                print(f'Beam #{i}: {beam}')

            history.append(beam)
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
                a.next_state.value = a.state.value + math.log(v)

            ns = [a.next_state for a in actions]
            ns.sort(key=lambda s: s.value, reverse=True)

            if debug:
                print(f'Candidates: {[(s, s.value) for s in ns[::-1]]}')

            beam = ns[:beam_size]
            history.append(beam)

        return success, history

    def recover_solutions(self, rollout_history):
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

class SuccessRatePolicyEvaluator:
    """Evaluates the policy derived from a Q function by its success rate at solving
       problems generated by an environment."""
    def __init__(self, environment, config):
        self.environment = environment
        self.seed = config.get('seed', 0)
        self.n_problems = config.get('n_problems', 100) # How many problems to use.
        self.max_steps = config.get('max_steps', 30) # Maximum length of an episode.
        self.beam_size = config.get('beam_size', 1) # Size of the beam in beam search.
        self.debug = config.get('debug', False) # Whether to print all steps during evaluation.

    def evaluate(self, q, verbose=False):
        successes, failures = [], []
        max_solution_length = 0

        for i in range(self.n_problems):
            problem = self.environment.generate_new(seed=(self.seed + i))
            success, history = q.rollout(self.environment, problem,
                                         self.max_steps, self.beam_size, self.debug)
            if success:
                successes.append(problem)
                max_solution_length = max(max_solution_length, len(history) - 1)
            else:
                failures.append(problem)
            if verbose:
                print(i, problem, '-- success?', success)

        return {
            'success_rate': len(successes) / self.n_problems,
            'max_solution_length': max_solution_length,
            'successes': successes,
            'failures': failures,
        }

class Environment:
    def __init__(self, url, default_domain=None):
        self.url = url
        self.default_domain = default_domain

    def generate_new(self, domain=None, seed=None):
        domain = domain or self.default_domain
        params = {'domain': domain}
        if seed is not None:
            params['seed'] = seed
        response = requests.post(self.url + '/generate', json=params).json()
        return State(response['state'], response['goals'], 0.0)

    def step(self, states, domain=None):
        domain = domain or self.default_domain
        response = requests.post(self.url + '/step',
                                 json={'domain': domain,
                                       'states': [s.facts for s in states],
                                       'goals': [s.goals for s in states]}).json()

        rewards = [int(r['success']) for r in response]
        actions = [[Action(state,
                           a['action'],
                           State(state.facts + (a['state'],), state.goals, 0.0),
                           0.0)
                    for a in r['actions']]
                   for state, r in zip(states, response)]

        for i, (s, sa) in enumerate(zip(states, actions)):
            s.value = rewards[i]
            for a in sa:
                a.next_state.parent_action = a

        return list(zip(rewards, actions))

class EndOfLearning(Exception):
    '''Exception used to signal the end of the learning budget for an agent.'''

class EnvironmentWithEvaluationProxy:
    '''Wrapper around the environment that triggers an evaluation every K calls'''
    def __init__(self, experiment_id, agent, environment, config={}):
        self.environment = environment
        self.n_steps = 0

        self.evaluate_every = config['evaluate_every']
        self.eval_config = config['eval_config']
        self.agent = agent
        self.output_path = config['output']
        self.checkpoint_path = config['checkpoint_path']
        self.max_steps = config['max_steps']
        self.print_every = config.get('print_every', 100)

        self.results = []
        self.n_new_problems = 0
        self.cumulative_reward = 0
        self.begin_time = datetime.datetime.now()
        self.n_checkpoints = 0

    def generate_new(self, domain=None, seed=None):
        self.n_new_problems += 1
        return self.environment.generate_new(domain, seed)

    def step(self, states, domain=None):
        n_steps_before = self.n_steps
        self.n_steps += len(states)

        # If the number of steps crossed the boundary of '0 mod evaluate_every', run evaluation.
        # If the agent took one step at a time, then we would only need to test if
        # n_steps % evaluate_every == 0. However the agent might take multiple steps at once.
        if (n_steps_before % self.evaluate_every) + len(states) >= self.evaluate_every:
            self.evaluate()

        if self.n_steps >= self.max_steps:
            # Budget ended.
            raise EndOfLearning()

        reward_and_actions = self.environment.step(states, domain)
        self.cumulative_reward += sum(rw for rw, _ in reward_and_actions)

        # Same logic as with evaluate_every.
        if (n_steps_before % self.print_every) + len(states) >= self.print_every:
            self.print_progress()

        return reward_and_actions

    def evaluate(self):
        print('Evaluating...')
        name, domain = self.agent.name(), self.environment.default_domain

        evaluator = SuccessRatePolicyEvaluator(self.environment, self.eval_config)
        results = evaluator.evaluate(self.agent.q_function)
        results['n_steps'] = self.n_steps
        results['name'] = name
        results['domain'] = domain
        results['problems_seen'] = self.n_new_problems
        results['cumulative_reward'] = self.cumulative_reward

        wandb.log({ 'success_rate': results['success_rate'],
                    'problems_seen': results['problems_seen'],
                    'n_environment_steps': results['n_steps'],
                    'cumulative_reward': results['cumulative_reward'],
                    'max_solution_length': results['max_solution_length'],
                   })

        print('Success rate:', results['success_rate'],
              '\tMax length:', results['max_solution_length'])

        output_path = self.output_path.format(self.experiment_id)

        try:
            with open(output_path, 'rb') as f:
                existing_results = pickle.load(f)
        except Exception as e:
            print(f'Starting new results log at {self.output_path} ({e})')
            existing_results = []

        existing_results.append(results)

        with open(self.output_path, 'wb') as f:
            pickle.dump(existing_results, f)

        torch.save(self.agent.q_function,
                   self.checkpoint_path.format(self.experiment_id,
                                               self.n_checkpoints))
        self.n_checkpoints += 1

    def evaluate_agent(self):
        self.evaluate()
        while True:
            try:
                self.agent.learn_from_environment(self)
            except EndOfLearning:
                print('Learning budget ended. Doing last learning round (if agent wants to)')
                self.agent.learn_from_experience()
                print('Running final evaluation...')
                self.evaluate()
                break
            except Exception as e:
                traceback.print_exc(e)
                print('Ignoring exception and continuing...')

    def print_progress(self):
        print('{} steps ({:.3}%, ETA: {}), {} total reward, explored {} problems. {}'
              .format(self.n_steps,
                      100 * (self.n_steps / self.max_steps),
                      util.format_eta(datetime.datetime.now() - self.begin_time,
                                      self.n_steps,
                                      self.max_steps),
                      self.cumulative_reward,
                      self.n_new_problems,
                      self.agent.stats()))

class DRRN(QFunction):
    def __init__(self, config, device):
        super().__init__()

        char_emb_dim = config.get('char_emb_dim', 128)
        self.hidden_dim = hidden_dim = config.get('hidden_dim', 256)
        self.lstm_layers = config.get('lstm_layers', 2)

        self.state_vocab = CharEncoding({ 'embedding_dim': char_emb_dim })
        self.action_vocab = CharEncoding({ 'embedding_dim': char_emb_dim })
        self.state_encoder = nn.LSTM(char_emb_dim, hidden_dim,
                                     self.lstm_layers, bidirectional=True)
        self.action_encoder = nn.LSTM(char_emb_dim, hidden_dim,
                                      self.lstm_layers, bidirectional=True)
        self.to(device)
        self.device = device

    def forward(self, actions):
        state_embedding = self.embed_states([a.state for a in actions])
        action_embedding = self.embed_actions(actions)
        q_values = (action_embedding * state_embedding).sum(dim=1).sigmoid()
        return q_values

    def embed_states(self, states):
        N, H = len(states), self.hidden_dim
        states = [s.facts[-1] for s in states]
        state_seq , _ = self.state_vocab.embed_batch(states, self.device)
        state_seq = state_seq.transpose(0, 1)
        _, (state_hn, state_cn) = self.state_encoder(state_seq)
        state_embedding = (state_hn
                           .view(self.lstm_layers, 2, N, self.hidden_dim)[-1]
                           .permute((1, 2, 0)).reshape(N, 2*H))
        return state_embedding

    def embed_actions(self, actions):
        actions = [a.action for a in actions]
        N, H = len(actions), self.hidden_dim
        actions_seq , _ = self.action_vocab.embed_batch(actions, self.device)
        actions_seq = actions_seq.transpose(0, 1)
        _, (actions_hn, actions_cn) = self.state_encoder(actions_seq)
        actions_embedding = (actions_hn
                             .view(self.lstm_layers, 2, N, self.hidden_dim)[-1]
                             .permute((1, 2, 0)).reshape((N, 2*H)))
        return actions_embedding


class LearnerValueFunctionAdapter(QFunction):
    '''Adapter for the legacy LearnerValueFunction class to be used as a QFunction.'''

    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, actions):
        s = [a.state.facts[-1] for a in actions]
        a = [a.action for a in actions]
        return self.model(s, a)

    def embed_states(self, states):
        s = [s.facts[-1] for s in states]
        return self.model.embed_state(s)

    def embed_actions(self, actions):
        return self.model.embed_action([a.action for a in actions])

class RandomQFunction(QFunction):
    def __init__(self):
        super().__init__()

    def forward(self, actions):
        return torch.rand(len(actions))

class LearningAgent:
    '''Algorithm that guides learning via interaction with the enviroment.
    Gets to decide when to start a new problem, what states to expand, when to take
    random actions, etc.

    Any learning algorithm can be combined with any Q-Function.
    '''
    def learn_from_environment(self, environment):
        "Lets the agent learn by interaction using any algorithm."
        raise NotImplementedError()

    def learn_from_experience(self):
        "Lets the agent optionally learn from its past interactions one last time before eval."

    def stats(self):
        "Returns a string with learning statistics for this agent, for debugging."
        return ""

class BeamSearchIterativeDeepening(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function
        self.replay_buffer_size = config['replay_buffer_size']

        self.replay_buffer_pos = collections.deque(self.replay_buffer_size)
        self.replay_buffer_neg = collections.deque(self.replay_buffer_size)
        self.training_problems_solved = 0

        self.max_depth = config['max_depth']
        self.depth_step = config['depth_step']
        self.initial_depth = config['initial_depth']
        self.step_every = config['step_every']
        self.beam_size = config['beam_size']

        self.balance_examples = config.get('balance_examples', True)
        self.optimize_on = config.get('optimize_on', 'problem')
        self.reward_decay = config.get('reward_decay', 1.0)
        self.batch_size = config.get('batch_size', 64)
        self.optimize_every = config.get('optimize_every', 1)
        self.n_gradient_steps = config.get('n_gradient_steps', 10)
        self.discard_unsolved_problems = config.get('discard_unsolved', False)
        self.add_success_action = config.get('add_success_action', False)
        self.full_imitation_learning = config.get('full_imitation_learning', False)

        self.optimizer = torch.optim.Adam(q_function.parameters(),
                                          lr=config.get('learning_rate', 1e-4))

    def name(self):
        if self.full_imitation_learning:
            return 'ImitationLearning'
        elif self.depth_step == 0 and not self.balance_examples:
            return 'DAgger'
        elif self.depth_step > 0 and not self.balance_examples:
            return 'IDDagger'
        elif self.depth_step > 0 and self.balance_examples:
            return 'IDCDagger'

    def learn_from_environment(self, environment):
        self.current_depth = self.initial_depth
        beam_size = self.beam_size
        step_every = self.step_every

        for i in itertools.count():
            problem = environment.generate_new()
            solution = self.beam_search(problem, environment)

            if solution is not None:
                self.training_problems_solved += 1

            if ((self.optimize_on == 'problem' and (i + 1) % self.optimize_every == 0) or
                (self.optimize_on == 'solution' and solution is not None and
                 self.training_problems_solved % self.optimize_every == 0)):
                logging.info('Running SGD steps.')
                self.gradient_steps()

            if (i + 1) % self.step_every == 0:
                self.current_depth = min(self.max_depth, self.current_depth + self.depth_step)
                logging.info(f'Beam search depth increased to {self.current_depth}.')

    def learn_from_experience(self):
        if self.full_imitation_learning:
            logging.info('Running Imitation learning')
            self.gradient_steps(True)

    def beam_search(self, state, environment):
        states_by_id = {id(state): state}
        state_parent_edge = {}
        beam = [state]
        solution = None # The state that we found that solves the problem.
        action_reward = {} # Remember rewards we attribute to each action.

        for i in range(self.current_depth):
            rewards, actions = zip(*environment.step(beam))

            for s, r, state_actions in zip(beam, rewards, actions):
                for a in state_actions:
                    # Remember how we got to this state.
                    states_by_id[id(a.next_state)] = a.next_state
                    state_parent_edge[id(a.next_state)] = (s, a)
                # Record solution, if found.
                if r:
                    if self.add_success_action:
                        states_by_id[id(SUCCESS_STATE)] = SUCCESS_STATE
                        state_parent_edge[id(SUCCESS_STATE)] = Action(s,
                                                                      'success',
                                                                      SUCCESS_STATE,
                                                                      1.0,
                                                                      1.0)
                        solution = SUCCESS_STATE
                    else:
                        solution = s

            if solution is not None:
                # Traverse all the state -> next_state edges backwards, remembering
                # all states in the path to the solution.
                current = solution
                current_reward = 1.0

                while id(current) in state_parent_edge:
                    prev_s, a = state_parent_edge[id(current)]
                    action_reward[id(a)] = current_reward
                    current_reward *= self.reward_decay
                    current = prev_s

                break

            all_actions = [a for state_actions in actions for a in state_actions]

            if not len(all_actions):
                break

            # Query model, sort next states by value, then update beam.
            with torch.no_grad():
                q_values = self.q_function(all_actions)
                q_values = q_values.tolist()

            for a, v in zip(all_actions, q_values):
                a.value = v

            next_states = []
            for s, state_actions in zip(beam, actions):
                for a in state_actions:
                    ns = a.next_state
                    next_states.append(ns)
                    ns.value = s.value + math.log(a.value)

            next_states.sort(key=lambda s: s.value, reverse=True)
            beam = next_states[:self.beam_size]

        # Add all edges traversed as examples in the experience replay buffer.
        if solution is not None or not self.discard_unsolved_problems:
            for s, (parent, a) in state_parent_edge.items():
                r = action_reward.get(id(a), 0.0)
                b = self.replay_buffer_pos if r > 0 else self.replay_buffer_neg
                b.append((states_by_id[s], a, r))

        return solution

    def stats(self):
        return "replay buffer size = {}, {} positive".format(
            len(self.replay_buffer_pos) + len(self.replay_buffer_neg),
            len(self.replay_buffer_pos))

    def gradient_steps(self, is_last_round=False):
        if self.full_imitation_learning and not is_last_round:
            return

        if self.balance_examples:
            n_each = min(len(self.replay_buffer_pos), len(self.replay_buffer_neg))
            examples = (random.sample(self.replay_buffer_pos, k=n_each) +
                        random.sample(self.replay_buffer_neg, k=n_each))
        else:
            examples = self.replay_buffer_pos + self.replay_buffer_neg

        logging.info(f'Taking {self.n_gradient_steps} with {len(examples)} examples (balanced = {self.balance_examples})')
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        for i in range(self.n_gradient_steps):
            batch = random.sample(examples, batch_size)
            batch_s, batch_a, batch_r = zip(*batch)

            self.optimizer.zero_grad()

            r_pred = self.q_function(batch_a)
            loss = F.binary_cross_entropy(r_pred, torch.tensor(batch_r,
                                                               dtype=r_pred.dtype,
                                                               device=r_pred.device))
            wandb.log({ 'train_loss': loss.item() })
            loss.backward()
            self.optimizer.step()

# A tuple of the replay buffer. We don't need to store the current state or the next state
# because a0 is an Action object, which already has a0.state and a0.next_state.
QReplayBufferTuple = collections.namedtuple('QReplayBufferTuple',
                                            ['a0', 'r', 'A1'])

class QLearning(LearningAgent):
    def __init__(self, q_function, config):
        self.q_function = q_function

        self.replay_buffer_size = config['replay_buffer_size']
        self.max_depth = config['max_depth']

        self.discount_factor = config.get('discount_factor', 1.0)
        self.batch_size = config.get('batch_size', 64)
        self.softmax_alpha = config.get('softmax_alpha', 1.0)

        self.replay_buffer = collections.deque(maxlen=self.replay_buffer_size)
        self.solutions_found = 0

        self.optimizer = torch.optim.Adam(q_function.parameters(),
                                          lr=config.get('learning_rate', 1e-4))

    def name(self):
        return 'QLearning'

    def learn_from_environment(self, environment):
        for i in itertools.count():
            state = environment.generate_new()
            r, actions = environment.step([state])[0]

            if r:
                # Trivial state: already solved, no examples to draw.
                continue

            for j in range(self.max_depth):
                # No actions to take.
                if not len(actions):
                    break

                with torch.no_grad():
                    q_values = self.q_function(actions)
                    pi = Categorical(logits=self.softmax_alpha * q_values)
                    a = pi.sample().item()

                s_next = actions[a].next_state
                r, next_actions = environment.step([s_next])[0]
                self.replay_buffer.append(QReplayBufferTuple(actions[a],
                                                             r,
                                                             next_actions))
                self.gradient_steps()

    def learn_from_experience(self):
        pass # QLearning doesn't have a learning step at the end.

    def stats(self):
        return "replay buffer size = {}, {} solutions found".format(
            len(self.replay_buffer), self.solutions_found)

    def gradient_steps(self):
        examples = self.replay_buffer
        batch_size = min(self.batch_size, len(examples))

        if batch_size == 0:
            return

        batch = random.sample(examples, batch_size)
        ys = []

        # Compute ys.
        with torch.no_grad():
            for t in batch:
                if t.r > 0: # Next state is terminal.
                    ys.append(t.r)
                else:
                    # Need to compute maximum Q value for all actions.
                    max_q = self.q_function(t.A1).max()
                    ys.append(t.r + self.discount_factor * max_q)

        # Compute Q estimates and take gradient steps.
        self.optimizer.zero_grad()
        q_estimates = self.q_function([t.a0 for t in batch])

        y = torch.tensor(ys, dtype=q_estimates.dtype, device=q_estimates.device)
        loss = ((y - q_estimates)**2).mean()
        wandb.log({ 'train_loss': loss.item() })
        loss.backward()
        self.optimizer.step()

def evaluate_policy(config, device):
    if config.get('random_policy'):
        q = RandomQFunction()
    else:
        q = torch.load(config['model_path'], map_location=device)

    if isinstance(q, LearnerValueFunction):
        q.encoding.max_line_length = 100
        q = LearnerValueFunctionAdapter(q)

    q.to(device)
    q.device = device

    domain = config['domain']
    env = Environment(config['environment_url'], domain)
    evaluator = SuccessRatePolicyEvaluator(env, config.get('eval_config', {}))
    result = evaluator.evaluate(q, verbose=True)

    print('Success rate:', result['success_rate'])
    print('Max solution length:', result['max_solution_length'])
    print('Solved problems:', result['successes'])
    print('Unsolved problems:', result['failures'])

def run_agent_experiment(config, device):
    experiment_id = config['experiment_id']
    domain = config['domain']
    agent_name = config['agent']['name']

    run_id = "{}-{}-{}".format(experiment_id, agent_name, domain)

    wandb.init(id=run_id,
               name=run_id,
               config=config,
               project='solver-agent',
               reinit=True)

    env = Environment(config['environment_url'], domain)

    if config['q_function']['type'] == 'DRRN':
        q_fn = DRRN(config['q_function'], device)

    if config['agent']['type'] == 'QLearning':
        agent = QLearning(q_fn, config['agent'])
    else:
        agent = BeamSearchIterativeDeepening(q_fn, config['agent'])

    print('Running', agent.name(), 'on', domain)

    eval_env = EnvironmentWithEvaluationProxy(run_id, agent, env, config['eval_environment'])
    eval_env.evaluate_agent()

def run_batch_experiment(config):
    'Spawns a series of processes to run experiments for each agent/domain pair.'
    experiment_id = util.random_id()
    domains = config['domains']
    agents = config['agents']

    environment_port_base = config.get('environment_port_base', 9876)
    run_processes = []
    environments = []
    agent_index = 0
    gpus = config['gpus']

    print('Starting experiment', experiment_id)

    try:
        for domain in domains:
            for agent in agents:
                print(f'Running {agent["name"]} on {domain}')

                port = environment_port_base + agent_index
                environment_process = subprocess.Popen(
                    ['racket', 'environment.rkt', '-p', str(port)],
                    stderr=subprocess.DEVNULL)
                environments.append(environment_process)

                # Wait for environment to be ready.
                time.sleep(30)

                run_config = {
                    'experiment_id': experiment_id,
                    'environment_url': 'http://localhost:{}'.format(port),
                    'agent': agent,
                    'domain': domain,
                    'q_function': config['q_function'],
                    'eval_environment': copy.deepcopy(config['eval_environment'])
                }

                print('Running agent with config', json.dumps(run_config))

                agent_process = subprocess.Popen(
                    ['python3', 'agent.py', '--learn', '--config', json.dumps(run_config),
                     '--gpu', str(gpus[agent_index % len(gpus)])],
                    stderr=subprocess.DEVNULL)
                run_processes.append(agent_process)

                agent_index += 1
    except (Exception, KeyboardInterrupt) as e:
        print('Killing all created processes...')
        for p in run_processes + environments:
            p.terminate()

        raise

    print('Waiting for all agents to finish...')
    for p in run_processes:
        p.wait()
    print('Shutting down environments...')
    for p in environments:
        p.terminate()
    print('Done!')

def interact():
    env = Environment('http://localhost:9898')
    breakpoint()
    print('REPL')

if __name__ == '__main__':
    parser = argparse.ArgumentParser("Train RL agents to solve symbolic domains")
    parser.add_argument('--config', help='Path to config file, or inline JSON.')
    parser.add_argument('--learn', help='Put an agent to learn from the environment', action='store_true')
    parser.add_argument('--experiment', help='Run a batch of experiments with multiple agents and environments',
                        action='store_true')
    parser.add_argument('--eval', help='Evaluate a learned policy', action='store_true')
    parser.add_argument('--repl', help='Get a REPL with an environment', action='store_true')
    parser.add_argument('--debug', help='Enable debug messages.', action='store_true')
    parser.add_argument('--gpu', type=int, default=None, help='Which GPU to use.')

    opt = parser.parse_args()

    try:
        if opt.config:
            config = json.loads(opt.config)
    except json.decoder.JSONDecodeError:
        config = json.load(open(opt.config))

    device = torch.device('cpu') if not opt.gpu else torch.device(opt.gpu)

    # configure logging.
    FORMAT = '%(asctime)-15s %(message)s'
    logging.basicConfig(format=FORMAT)

    if opt.debug:
        logging.getLogger().setLevel(logging.INFO)

    # Only shown in debug mode.
    logging.info('Running in debug mode.')

    if opt.learn:
        run_agent_experiment(config, device)
    elif opt.eval:
        evaluate_policy(config, device)
    elif opt.repl:
        interact()
    elif opt.experiment:
        run_batch_experiment(config)
