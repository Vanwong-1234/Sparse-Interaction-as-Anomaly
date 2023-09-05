import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
from src.components.episode_buffer import EpisodeBatch
from src.modules.mixers.vdn import VDNMixer
from src.modules.mixers.qmix import QMixer
import torch as th
from torch.optim import RMSprop
from torch.distributions import Categorical


class EITILearner:
    def __init__(self, mac, scheme, logger, args):
        self.args = args
        self.mac = mac
        self.logger = logger

        self.params = list(mac.parameters())

        raise Exception('EITI has not been achieved.')

        self.last_target_update_episode = 0

        self.mixer = None
        if args.mixer is not None:
            if args.mixer == "vdn":
                self.mixer = VDNMixer()
            elif args.mixer == "qmix":
                self.mixer = QMixer(args)
            else:
                raise ValueError("Mixer {} not recognised.".format(args.mixer))
            self.params += list(self.mixer.parameters())
            self.target_mixer = copy.deepcopy(self.mixer)

        self.optimiser = RMSprop(params=self.params, lr=args.lr, alpha=args.optim_alpha, eps=args.optim_eps)

        # a little wasteful to deepcopy (e.g. duplicates action selector), but should work for any MAC
        self.target_mac = copy.deepcopy(mac)

        self.log_stats_t = -self.args.learner_log_interval - 1

    def train(self, batch: EpisodeBatch, t_env: int, episode_num: int):
        # Get the relevant quantities
        rewards = batch["reward"][:, :-1]
        actions = batch["actions"][:, :-1]
        terminated = batch["terminated"][:, :-1].float()
        mask = batch["filled"][:, :-1].float()
        mask[:, 1:] = mask[:, 1:] * (1 - terminated[:, :-1])
        avail_actions = batch["avail_actions"]

        # Calculate estimated Q-Values
        mac_out = []
        self.mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            agent_outs = self.mac.forward(batch, t=t)
            mac_out.append(agent_outs)
        mac_out = th.stack(mac_out, dim=1)  # Concat over time

        mac_out_dist = Categorical(logits=mac_out.clone().detach()).entropy()

        # Pick the Q-Values for the actions taken by each agent
        chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)  # Remove the last dim

        # Calculate the Q-Values necessary for the target
        target_mac_out = []
        self.target_mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            target_agent_outs = self.target_mac.forward(batch, t=t)
            target_mac_out.append(target_agent_outs)

        # We don't need the first timesteps Q-Value estimate for calculating targets
        target_mac_out = th.stack(target_mac_out[1:], dim=1)  # Concat across time

        # Mask out unavailable actions
        target_mac_out[avail_actions[:, 1:] == 0] = -9999999  # From OG deepmarl

        # Max over target Q-Values
        if self.args.double_q:
            # Get actions that maximise live Q (for double q-learning)
            mac_out_detach = mac_out.clone().detach()
            mac_out_detach[avail_actions == 0] = -9999999
            cur_max_actions = mac_out_detach[:, 1:].max(dim=3, keepdim=True)[1]
            target_max_qvals = th.gather(target_mac_out, 3, cur_max_actions).squeeze(3)
        else:
            target_max_qvals = target_mac_out.max(dim=3)[0]
        target_qvals_show = target_max_qvals.clone()

        # Mix
        if self.mixer is not None:
            chosen_action_qvals = self.mixer(chosen_action_qvals, batch["state"][:, :-1])
            target_max_qvals = self.target_mixer(target_max_qvals, batch["state"][:, 1:])

        # Calculate 1-step Q-Learning targets
        targets = rewards + self.args.gamma * (1 - terminated) * target_max_qvals
        # print((targets == rewards).all())

        # Td-error
        td_error = (chosen_action_qvals - targets.detach())

        mask = mask.expand_as(td_error)

        # 0-out the targets that came from padded data
        masked_td_error = td_error * mask

        # Normal L2 loss, take mean over actual data
        loss = (masked_td_error ** 2).sum() / mask.sum()

        # Optimise
        self.optimiser.zero_grad()
        loss.backward()
        grad_norm = th.nn.utils.clip_grad_norm_(self.params, self.args.grad_norm_clip)
        self.optimiser.step()

        if (episode_num - self.last_target_update_episode) / self.args.target_update_interval >= 1.0:
            self._update_targets()
            self.last_target_update_episode = episode_num

        if t_env - self.log_stats_t >= self.args.learner_log_interval:
            self.logger.log_stat("loss", loss.item(), t_env)
            self.logger.log_stat("grad_norm", grad_norm, t_env)
            mask_elems = mask.sum().item()
            self.logger.log_stat("td_error_abs", (masked_td_error.abs().sum().item()/mask_elems), t_env)
            self.logger.log_stat("q_taken_mean", (chosen_action_qvals * mask).sum().item()/(mask_elems * self.args.n_agents), t_env)
            self.logger.log_stat("target_mean", (targets * mask).sum().item()/(mask_elems * self.args.n_agents), t_env)
            agent_utils = (th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3) * mask).sum().item() / (mask_elems * self.args.n_agents)
            self.logger.log_stat("agent_utils", agent_utils, t_env)
            self.logger.log_stat("agent_target",
                                 (target_qvals_show * mask).sum().item() / (mask_elems * self.args.n_agents), t_env)
            self.logger.log_stat("entropy_mean", mac_out_dist.mean().item(), t_env)
            self.logger.log_stat("entropy_std", mac_out_dist.std().item(), t_env)
            # self.logger.log_stat("action0_targets_mean", (targets[actions[:, :, 0, :] == 0]).mean().item(), t_env)
            # self.logger.log_stat("action0_prop", (actions[:, :, 0, :] == 0).float().sum() / len(actions), t_env)
            self.log_stats_t = t_env
            # print(t_env, targets[actions[:, :, 0, :] == 0])
            # print(t_env, (targets[actions[:, :, 0, :] == 0]).mean())

    def _update_targets(self):
        self.target_mac.load_state(self.mac)
        if self.mixer is not None:
            self.target_mixer.load_state_dict(self.mixer.state_dict())
        self.logger.console_logger.info("Updated target network")

    def cuda(self):
        self.mac.cuda()
        self.target_mac.cuda()
        if self.mixer is not None:
            self.mixer.to(self.args.device)
            self.target_mixer.to(self.args.device)

    def save_models(self, path):
        self.mac.save_models(path)
        if self.mixer is not None:
            th.save(self.mixer.state_dict(), "{}/mixer.th".format(path))
        th.save(self.optimiser.state_dict(), "{}/opt.th".format(path))

    def load_models(self, path):
        self.mac.load_models(path)
        # Not quite right but I don't want to save target networks
        self.target_mac.load_models(path)
        if self.mixer is not None:
            self.mixer.load_state_dict(th.load("{}/mixer.th".format(path), map_location=lambda storage, loc: storage))
        self.optimiser.load_state_dict(th.load("{}/opt.th".format(path), map_location=lambda storage, loc: storage))

    def show_matrix_info(self, batch, t_env):
        mac_out = []
        self.mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            # agent_outs = self.mac.forward(batch, t=t, show_h=bool(1-t))
            agent_outs = self.mac.forward(batch, t=t)
            mac_out.append(agent_outs)
        mac_out = th.stack(mac_out, dim=1)  # Concat over time, threads, steps, agents, actions
        actions_dim = mac_out.shape[3]
        print("Episode %i, The learned matrix payoff is:" % t_env)
        payoff = ""
        for ai in range(actions_dim):
            for aj in range(actions_dim):
                actions = th.tensor([[ai, aj]]).to(**dict(dtype=th.int64, device=mac_out.device))
                actions = actions.unsqueeze(0).unsqueeze(-1).repeat(mac_out.shape[0], mac_out.shape[1]-1, 1, 1)
                # print(actions.shape, actions)
                chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)
                if self.mixer is not None:
                    # if ai == 0 and aj == 0:
                    #     mixer_qvals = self.mixer(chosen_action_qvals, batch["state"][:, :-1], True)
                    # else:
                    mixer_qvals = self.mixer(chosen_action_qvals, batch["state"][:, :-1])
                else:
                    mixer_qvals = th.zeros((1, 1, 1))
                sp = "{0:.4}".format(str(chosen_action_qvals[0, 0, 0].item())) + "||" \
                     + "{0:.4}".format(str(chosen_action_qvals[0, 0, 1].item())) \
                     + "||" + "{0:.4}".format(str(mixer_qvals[0, 0, 0].item())) + "     "
                payoff += sp
                # print(ai, aj, chosen_action_qvals[0, 0, 0].item(),
                # chosen_action_qvals[0, 0, 1].item(), mixer_qvals[0, 0, 0].item())
            payoff += "\n"
        print(payoff)
        # max_actions = mac_out.max(dim=3)[1]
        max_actions = batch["actions"][:, :-1, :, 0]
        print("Max actions is:", max_actions[0, 0, 0].item(), max_actions[0, 0, 1].item(),
              "  ||   Reward is", batch["reward"][0, 0, 0].item())
        # chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)
        # chosen_action_qvals = self.mixer(chosen_action_qvals, batch["state"][:, :-1])
        # print(mac_out.shape)
        # print(mac_out)
        # print(batch["actions"][:, :-1])
        # print(batch["actions"][:, :-1].shape)
        # exit()
        # print(self.mixer.state_dict())

    def show_mmdp_info(self, batch, t_env):
        pass