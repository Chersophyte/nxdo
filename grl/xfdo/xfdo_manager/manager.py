from abc import ABC, abstractmethod
import numpy as np
from copy import deepcopy
from threading import RLock
from itertools import product
from typing import List, Tuple, Union, Dict, Callable
import json
import os
from grl.p2sro.payoff_table import PayoffTable, PayoffTableStrategySpec
from grl.utils import datetime_str


class RestrictedGameSolveResult:
    def __init__(self,
                 latest_metanash_spec_for_each_player: Tuple[PayoffTableStrategySpec],
                 episodes_spent_in_solve: int,
                 timesteps_spent_in_solve: int,
                 extra_data_to_log: dict):
        self.latest_metanash_spec_for_each_player = latest_metanash_spec_for_each_player
        self.episodes_spent_in_solve = episodes_spent_in_solve
        self.timesteps_spent_in_solve = timesteps_spent_in_solve
        self.extra_data_to_log = extra_data_to_log


class SolveRestrictedGame(ABC):

    @abstractmethod
    def __call__(self,
                 log_dir: str,
                 br_spec_lists_for_each_player: Dict[int, List[PayoffTableStrategySpec]]) -> RestrictedGameSolveResult:
        pass

class XFDOManager(object):

    def __init__(self,
                 solve_restricted_game: SolveRestrictedGame,
                 n_players: int = 2,
                 log_dir: str = None):

        self._solve_restricted_game = solve_restricted_game

        if n_players != 2:
            raise NotImplementedError
        self._n_players = n_players

        if log_dir is None:
            log_dir = f"/tmp/xfdo_{datetime_str()}"
        self.log_dir = log_dir
        self._json_log_path = os.path.join(self.log_dir, "manager_results.json")
        print(f"Manager log dir is {self.log_dir}")

        self._current_double_oracle_iteration = 0
        self._player_brs_are_finished_this_iter = {p: False for p in range(self._n_players)}
        self._br_spec_lists_for_each_player: Dict[int, List[PayoffTableStrategySpec]] = {p: [] for p in
                                                                                    range(self._n_players)}

        self._episodes_count = 0
        self._timesteps_count = 0

        self._next_iter_br_spec_lists_for_each_player = deepcopy(self._br_spec_lists_for_each_player)

        self._latest_metanash_spec_for_each_player: List[PayoffTableStrategySpec] = [None, None]

        self._modification_lock = RLock()

    def n_players(self) -> int:
        return self._n_players

    def get_log_dir(self) -> str:
        return self.log_dir

    def claim_new_active_policy_for_player(self, player) -> Union[
        Tuple[Dict[int, PayoffTableStrategySpec], Dict[int, List[PayoffTableStrategySpec]], int],
        Tuple[None, None, None]
    ]:
        with self._modification_lock:
            if player < 0 or player >= self._n_players:
                raise ValueError(f"player {player} is out of range. Must be in [0, n_players).")

            if self._player_brs_are_finished_this_iter[player]:
                return None, None, None

            metanash_specs_for_players = {}
            delegate_specs_for_players = {}
            for other_player, latest_metanash_spec in enumerate(self._latest_metanash_spec_for_each_player):
                metanash_specs_for_players[other_player] = latest_metanash_spec
                delegate_specs_for_players[other_player] = self._br_spec_lists_for_each_player[other_player]

            return (metanash_specs_for_players,
                    delegate_specs_for_players,
                    self._current_double_oracle_iteration)

    def submit_final_br_policy(self, player, policy_num, metadata_dict):
        with self._modification_lock:
            if player < 0 or player >= self._n_players:
                raise ValueError(f"player {player} is out of range. Must be in [0, n_players).")
            if policy_num != self._current_double_oracle_iteration:
                raise ValueError(f"Policy {policy_num} isn't the same as the current double oracle iteration "
                                 f"{self._current_double_oracle_iteration}.")

            br_policy_spec: PayoffTableStrategySpec = PayoffTableStrategySpec(
                strategy_id=self._strat_id(player=player, policy_num=policy_num),
                metadata=metadata_dict,
                pure_strategy_indexes={player: policy_num}
            )

            self._episodes_count += metadata_dict["episodes_training_br"]
            self._timesteps_count += metadata_dict["timesteps_training_br"]

            self._next_iter_br_spec_lists_for_each_player[player].append(br_policy_spec)
            self._player_brs_are_finished_this_iter[player] = True

            all_players_finished_brs_this_ter = all(self._player_brs_are_finished_this_iter.values())
            if all_players_finished_brs_this_ter:
                self._br_spec_lists_for_each_player = deepcopy(self._next_iter_br_spec_lists_for_each_player)

                print("Solving restricted game")
                game_solve_result = self._solve_restricted_game(
                    log_dir=self.log_dir, br_spec_lists_for_each_player=self._next_iter_br_spec_lists_for_each_player)

                self._latest_metanash_spec_for_each_player = game_solve_result.latest_metanash_spec_for_each_player
                self._episodes_count += game_solve_result.episodes_spent_in_solve
                self._timesteps_count += game_solve_result.timesteps_spent_in_solve

                data_to_log = {
                        "episodes_total": self._episodes_count,
                        "timesteps_total": self._timesteps_count,
                    }
                assert "episodes_total" not in game_solve_result.extra_data_to_log
                assert "timesteps_total" not in game_solve_result.extra_data_to_log
                data_to_log.update(game_solve_result.extra_data_to_log)
                with open(self._json_log_path, "+a") as json_file:
                    json_file.writelines([json.dumps(data_to_log)+'\n'])

                self._current_double_oracle_iteration += 1
                self._player_brs_are_finished_this_iter = {p: False for p in range(self._n_players)}

    def is_policy_fixed(self, player, policy_num) -> bool:
        with self._modification_lock:
            if player < 0 or player >= self._n_players:
                raise ValueError(f"player {player} is out of range. Must be in [0, n_players).")
            if policy_num < self._current_double_oracle_iteration:
                return True
            elif policy_num == self._current_double_oracle_iteration:
                return self._player_brs_are_finished_this_iter[player]
            else:
                raise ValueError(f"Policy {policy_num} isn't a fixed or active policy for player {player}. "
                                 f"The current double oracle iteration is {self._current_double_oracle_iteration}.")

    @staticmethod
    def _strat_id(player, policy_num) -> str:
        return f"player_{player}_policy_{policy_num}"
