from time import time, sleep

from ..core import Agent
from ..utils import *

import optunity as ot
import pandas as pd
import talib as tl
from decimal import Decimal
from datetime import timedelta
from numpy import diag, sqrt, log, trace
from numpy.linalg import inv

from ..exchange_api.poloniex import ExchangeError, RetryException

import scipy
from scipy.signal import argrelextrema
import cvxopt as opt
opt.solvers.options['show_progress'] = False


# TODO LIST
# HEADING

# Base class
class APrioriAgent(Agent):
    """
    Apriori abstract trading agent.
    Use this class to create trading strategies and deploy to Trading environment
    to train and deploy models directly into the market
    """
    def __init__(self, fiat, name=""):
        """

        :param fiat: str: symbol to use as quote
        :param name: str: agent name
        """
        super().__init__()
        self.epsilon = 1e-16
        self.fiat = fiat
        self.step = 0
        self.name = name
        self.log = {}

    # Model methods
    def predict(self, obs):
        """
        Select action on actual observation
        :param obs:
        :return:
        """
        raise NotImplementedError()

    def rebalance(self, obs):
        return NotImplementedError()

    def get_portfolio_vector(self, obs, index=-1):
        """
        Calculate portfolio vector from observation
        :param obs: pandas DataFrame: Observation
        :param index: int: Index to vector retrieve. -1 = last
        :return: numpy array: Portfolio vector with values ranging [0, 1] and norm 1
        """
        coin_val = {}
        for symbol in obs.columns.levels[0]:
            if symbol not in self.fiat:
                coin_val[symbol.split("_")[1]] = obs.get_value(obs.index[index], (symbol, symbol.split("_")[1])) * \
                                                 obs.get_value(obs.index[index], (symbol, 'open'))

        portval = 0
        for symbol in coin_val:
            portval += coin_val[symbol]
        portval += obs[self.fiat].iloc[index].values

        port_vec = np.zeros(obs.columns.levels[0].shape)
        for i, symbol in enumerate(coin_val):
            port_vec[i] = safe_div(coin_val[symbol], portval)

        port_vec[-1] = safe_div(obs[self.fiat].iloc[index].values, portval)

        return port_vec

    # Train methods
    def set_params(self, **kwargs):
        raise NotImplementedError("You must overwrite this class in your implementation.")

    def test(self, env, nb_episodes=1, action_repetition=1, callbacks=None, visualize=False, start_step=0,
             nb_max_episode_steps=None, nb_max_start_steps=0, start_step_policy=None, verbose=False):
        """
        Test agent on environment
        """
        try:
            # Get env params
            self.fiat = env._fiat

            # Reset observations
            env.reset_status()
            obs = env.reset(reset_dfs=True)

            # Run start steps
            # Run start steps
            for i in range(nb_max_start_steps):
                obs, _, _, status = env.step(start_step_policy.rebalance(obs))
                if status['OOD']:
                    return 0.0

            # Get max episode length
            if nb_max_episode_steps is None:
                nb_max_episode_steps = env.data_length

            #Reset counters
            t0 = time()
            self.step = start_step
            episode_reward = 0.0
            while True:
                try:
                    # Take actions
                    action = self.rebalance(obs)
                    obs, reward, _, status = env.step(action)

                    # Accumulate returns and regret
                    episode_reward += safe_div(reward, env.portfolio_df.portval.astype('f').std())

                    # Increment step counter
                    self.step += 1

                    if visualize:
                        env.render()

                    if verbose:
                        print(">> step {0}/{1}, {2} % done, Cumulative Reward: {3}, ETC: {4}, Samples/s: {5:.04f}                        ".format(
                            self.step,
                            nb_max_episode_steps - env.obs_steps - 2,
                            int(100 * self.step / (nb_max_episode_steps - env.obs_steps - 2)),
                            episode_reward,
                            str(pd.to_timedelta((time() - t0) * ((nb_max_episode_steps - env.obs_steps - 2)
                                                                 - self.step), unit='s')),
                            1 / (time() - t0)
                        ), end="\r", flush=True)
                        t0 = time()

                    if status['OOD'] or self.step == nb_max_episode_steps:
                        return episode_reward

                    if status['Error']:
                        e = status['Error']
                        print("Env error:",
                              type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                        break

                except Exception as e:
                    print("Model Error:",
                          type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                    raise e

        except TypeError:
            print("\nYou must fit the model or provide indicator parameters in order to test.")

        except KeyboardInterrupt:
            print("\nKeyboard Interrupt: Stoping backtest\nElapsed steps: {0}/{1}, {2} % done.".format(self.step,
                                                                             nb_max_episode_steps,
                                                                             int(100 * self.step / nb_max_episode_steps)))
            return 0.0

    def fit(self, env, nb_steps, batch_size, search_space, constraints=None, action_repetition=1, callbacks=None, verbose=1,
            visualize=False, nb_max_start_steps=0, start_step_policy=None, log_interval=10000,
            nb_max_episode_steps=None):
        """
        Fit the model on parameters on the environment
        :param env: BacktestEnvironment instance
        :param nb_steps: Number of optimization evals
        :param batch_size: Size of the batch for each optimization pass
        :param search_space: Parameter search space
        :param constrains: Function returning False when constrains are violated
        :param action_repetition:
        :param callbacks:
        :param verbose:
        :param visualize:
        :param nb_max_start_steps:
        :param start_step_policy:
        :param log_interval:
        :param nb_max_episode_steps: Number of steps for one episode
        :return: tuple: Optimal parameters, information about the optimization process
        """
        try:
            # Initialize train
            env.training = True
            i = 0
            t0 = time()

            if verbose:
                print("Optimizing model for %d steps with batch size %d..." % (nb_steps, batch_size))

            ### First, optimize benchmark
            env.optimize_benchmark(nb_steps * 100, verbose=True)

            ## Now optimize model w.r.t benchmark
            # First define optimization constrains
            # Ex constrain:
            # @ot.constraints.constrained([lambda mean_type,
            #         ma1,
            #         ma2,
            #         std_span,
            #         alpha_up,
            #         alpha_down: ma1 < ma2])

            if not constraints:
                constraints = [lambda *args, **kwargs: True]

            # Then, define optimization routine
            @ot.constraints.constrained(constraints)
            @ot.constraints.violations_defaulted(-100)
            def find_hp(**kwargs):
                try:
                    # Init variables
                    nonlocal i, nb_steps, t0, env, nb_max_episode_steps

                    # Sample params
                    self.set_params(**kwargs)

                    # Try model for a batch
                    batch_reward = 0
                    for batch in range(batch_size):
                        # sample environment
                        r = self.test(env,
                                        nb_episodes=1,
                                        action_repetition=action_repetition,
                                        callbacks=callbacks,
                                        visualize=visualize,
                                        nb_max_episode_steps=nb_max_episode_steps,
                                        nb_max_start_steps=nb_max_start_steps,
                                        start_step_policy=start_step_policy,
                                        verbose=False)

                        # Accumulate reward
                        batch_reward += r

                    # Increment step counter
                    i += 1

                    # Update progress
                    if verbose:
                        print("Optimization step {0}/{1}, step reward: {2}, ETC: {3}                     ".format(i,
                                                                            nb_steps,
                                                                            batch_reward / batch_size,
                                                                            str(pd.to_timedelta((time() - t0) * (nb_steps - i), unit='s'))),
                              end="\r")
                        t0 = time()

                    # Average rewards and return
                    return batch_reward / batch_size

                except KeyboardInterrupt:
                    raise ot.api.fun.MaximumEvaluationsException(0)

            # Define params search space
            # Ex search space:
            #
            # hp = {
            #     'ma1': [2, env.obs_steps],
            #     'ma2': [2, env.obs_steps],
            #     'std_span': [2, env.obs_steps],
            #     'alpha_up': [1e-8, 1],
            #     'alpha_down': [1e-8, 1]
            #     }
            #
            # search_space = {'mean_type':{'simple': hp,
            #                              'exp': hp,
            #                              'kama': hp
            #                              }
            #                 }

            print("\nOptimizing model...")

            # Call optimizer
            opt_params, info, _ = ot.maximize_structured(find_hp,
                                              num_evals=nb_steps,
                                              search_space=search_space
                                              )

            # Update model params with optimal
            self.set_params(**opt_params)

            # Set flag off
            env.training = False

            # Return optimal params and information
            return opt_params, info

        except KeyboardInterrupt:

            # If interrupted, clean after yourself
            env.training = False
            print("\nOptimization interrupted by user.")
            return opt_params, info

    # Trade methods
    def trade(self, env, start_step=0, act_now=False, timeout=None, verbose=False, render=False, email=False, save_dir="./"):
        """
        TRADE REAL ASSETS WITHIN EXCHANGE. USE AT YOUR OWN RISK!
        :param env: Livetrading or Papertrading environment instance
        :param start_step: int: strategy start step
        :param act_now: bool: Whether to act now or at the next bar start
        :param timeout: int: Not implemented yet
        :param verbose: bool:
        :param render: bool: Not implemented yet
        :param email: bool: Wheter to send report email or not
        :param save_dir: str: Save directory for logs
        :return:
        """

        print("Executing paper trading with %d min frequency.\nInitial portfolio value: %f fiat units." % (env.period,
                                                                                            env.calc_total_portval()))

        # Fiat symbol
        self.fiat = env._fiat

        # Reset env and get initial env
        env.reset_status()
        obs = env.reset()

        try:
            # Init variables
            self.step = start_step

            action = np.zeros(len(env.symbols))
            status = env.status
            last_action_time = floor_datetime(env.timestamp, env.period)
            t0 = time()

            can_act = act_now # TODO: FALSE HERE
            may_report = False

            init_portval = env.calc_total_portval()
            init_time = env.timestamp
            episode_reward = 0
            reward = 0
            while True:
                try:
                    # Log action time
                    loop_time = env.timestamp
                    if loop_time >= last_action_time + timedelta(minutes=env.period):
                        can_act = True
                        try:
                            del self.log["Trade_incomplete"]
                        except Exception:
                            pass

                    # If can act, run strategy and step environment
                    if can_act:
                        action = self.rebalance(env.get_observation(True).astype(np.float64))
                        obs, reward, done, status = env.step(action)
                        episode_reward += reward

                        # If action is complete, increment step counter, log action time and allow report
                        if not done:
                            self.log["Trade_incomplete"] = "Position change was not fully completed."

                        self.step += 1
                        last_action_time = floor_datetime(env.timestamp, env.period)
                        can_act = False
                        may_report = True

                    # If cant act, just take a observation and return
                    else:
                        obs = env.get_observation(True).astype(np.float64)

                    # Not implemented yet
                    if render:
                        env.render()

                    # Report generation
                    if verbose or email:
                        msg = self.make_report(env, obs, reward, episode_reward, t0)

                        if verbose:
                            print(msg, end="\r", flush=True)

                        if email and may_report:
                            if hasattr(env, 'email'):
                                env.send_email("Trading report " + self.name, msg)
                            may_report = False

                    # If environment return an error,save data frames and break
                    if status['Error']:
                        e = status['Error']

                        self.save_dfs(env, save_dir, init_time)

                        if verbose:
                            print("Env error:",
                                  type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                        if email:
                            env.send_email("Trading error: %s" % env.name, env.parse_error(e))
                        break

                    # Wait for next bar open
                    try:
                        sleep(datetime.timestamp(last_action_time + timedelta(minutes=env.period))
                              - datetime.timestamp(env.timestamp) + int(np.random.random(1) * 30))
                    except ValueError:
                        sleep(1 + int(np.random.random(1) * 30))

                # If you've done enough tries, cancel action and wait for the next bar
                except RetryException as e:
                    if 'retryDelays exhausted' in e.__str__():
                        env.send_email("Trading error: %s" % env.name, env.parse_error(e))

                        try:
                            sleep(datetime.timestamp(last_action_time + timedelta(minutes=env.period))
                                  - datetime.timestamp(env.timestamp) + int(np.random.random(1) * 30))
                        except ValueError:
                            sleep(1 + int(np.random.random(1) * 30))
                    else:
                        raise e

                # Catch exceptions
                except Exception as e:
                    print("\nAgent Error:",
                          type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                    print(env.timestamp)
                    print(obs)
                    print(env.portfolio_df.iloc[-5:])
                    print(env.action_df.iloc[-5:])
                    print("Action taken:", action)

                    # Save dataframes for analysis
                    self.save_dfs(env, save_dir, init_time)

                    if email:
                        env.send_email("Trading error: %s" % env.name, env.parse_error(e))

                    break

        # If interrupted, save data and quit
        except KeyboardInterrupt:
            # Save dataframes for analysis
            self.save_dfs(env, save_dir, init_time)

            print("\nKeyboard Interrupt: Stoping cryptotrader" + \
                  "\nElapsed steps: {0}\nUptime: {1}\nInitial Portval: {2}\nFinal Portval: {3}\n".format(self.step,
                                                               str(pd.to_timedelta(time() - t0, unit='s')),
                                                               init_portval,
                                                               env.calc_total_portval()))

    def make_report(self, env, obs, reward, episode_reward, t0):
        """
        Report generator
        :param env:
        :param obs:
        :param reward:
        :return:
        """

        # Portfolio values
        try:
            init_portval = float(env.portfolio_df.get_value(env.portfolio_df.index[0], 'portval'))
            prev_portval = float(env.portfolio_df.get_value(env.portfolio_df.index[-2], 'portval'))
            last_portval = float(env.portfolio_df.get_value(env.portfolio_df.index[-1], 'portval'))
        except IndexError:
            init_portval = prev_portval = last_portval = float(env.portfolio_df.get_value(env.portfolio_df.index[0],
                                                                                          'portval'))

        # Returns summary
        msg = "\n>> Step {0}\nPortval: {1:.3f}\nStep Reward: {2:.6f}\nCumulative Reward: {3:.6f}\n".format(
            self.step,
            last_portval,
            reward,
            episode_reward,
            )

        msg += "\nStep portfolio change: %f" % (float(
            100 * safe_div(last_portval - prev_portval, prev_portval)
            )) + " %"

        msg += "\nAccumulated portfolio change: %f" % (float(
            100 * safe_div(last_portval - init_portval, init_portval)
            )) + " %\n"

        # Time summary
        msg += "\nAction time: {0}\nTstamp: {1}\nUptime: {2}\n".format(
            datetime.now(),
            str(obs.index[-1]),
            str(pd.to_timedelta(time() - t0, unit='s'))
            )

        # Prices summary
        msg += "\nPrices summary:\n"
        msg += "           Prev open:    Last price:    Pct change:\n"

        adm = 0.0
        k = 0
        for symbol in env.pairs:

            pp = obs.get_value(obs.index[-2], (symbol, 'open'))
            nep = obs.get_value(obs.index[-1], (symbol, 'close'))
            pc = 100 * safe_div((nep - pp), pp)
            adm += pc
            k += 1

            msg += "%-9s: %11.4f   %11.4f %11.2f" % (symbol, pp, nep, pc) + " %\n"

        msg += "Mean pct change: %5.02f %%\n" % (adm / k)

        # Action summary
        msg += "\nAction Summary:\n"
        try:
            pa = env.action_df.iloc[-3].astype(str).to_dict()
        except IndexError:
            pa = env.action_df.iloc[-1].astype(str).to_dict()
        la = env.action_df.iloc[-1].astype(str).to_dict()
        msg += "        Prev action:  Last Action:  Action diff:\n"
        for symbol in pa:
            if symbol is not "online":
                pac = 100 * float(pa[symbol])
                nac = 100 * float(la[symbol])
                ad = nac - pac

                msg += "%-6s:  %5.02f %%       %5.02f %%      %5.02f %%\n" % (symbol, pac, nac, ad)
            else:
                msg += "%s: %s          %s\n" % (symbol, pa[symbol], la[symbol])

        # Slippage summary
        msg += "\nSlippage summary:\n"
        try:
            sl = (100 * (env.action_df.iloc[-1] - env.action_df.iloc[-2])).drop('online').astype('f').\
                describe(percentiles=[0.95, 0.05]).astype(str).to_dict()
        except IndexError:
            sl = (100 * (env.action_df.iloc[-1] - env.action_df.iloc[-1])).drop('online').astype('f').\
                describe(percentiles=[0.95, 0.05]).astype(str).to_dict()
        for symbol in sl:
            if symbol is not 'count':
                msg += str(symbol) + ": " + sl[symbol] + '\n'

        # Operational status summary
        msg += "\nStatus: %s\n" % str(env.status)

        # Strategy log summary
        for key in self.log:
            if isinstance(self.log[key], dict):
                msg += '\n' + str(key) + '\n'
                for subkey in self.log[key]:
                    msg += str(subkey) + ": " + str(self.log[key][subkey]) + '\n'
            else:
                msg += '\n' + str(key) + ": " + str(self.log[key]) + '\n'

        return msg

    def save_dfs(self, env, save_dir, init_time):
        env.portfolio_df.to_json(save_dir +
                                 self.name + "_portfolio_df_" + str(env.period) + "min_" +
                                 str(init_time) + ".json")

        env.action_df.to_json(save_dir +
                              self.name + "_action_df_" + str(env.period) + "min_" +
                              str(init_time) + ".json")


# Test and benchmark
class TestAgent(APrioriAgent):
    """
    Test agent for debugging
    """
    def __repr__(self):
        return "Test"

    def __init__(self, obs_shape, fiat="USDT"):
        super().__init__(fiat)
        self.obs_shape = obs_shape

    def predict(self, obs):
        # Assert obs is valid
        assert obs.shape == self.obs_shape, "Wrong obs shape."

        for val in obs.applymap(lambda x: isinstance(x, Decimal) and Decimal.is_finite(x)).all():
            assert val, ("Non decimal value found in obs.", obs.applymap(lambda x: isinstance(x, Decimal) and Decimal.is_finite(x)).all())

        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)
        else:
            return self.get_portfolio_vector(obs)

    def rebalance(self, obs):
        return self.predict(obs.apply(convert_to.decimal, raw=True))

    def test(self, env, nb_episodes=1, action_repetition=1, callbacks=None, visualize=False,
             nb_max_episode_steps=None, nb_max_start_steps=0, start_step_policy=None, verbose=False):
        """
        Test agent on environment
        """
        try:
            # Get env params
            self.fiat = env._fiat

            # Reset observations
            env.reset_status()
            env.reset(reset_dfs=True)

            # Get max episode length
            if nb_max_episode_steps is None:
                nb_max_episode_steps = env.data_length

            #Reset counters
            t0 = time()
            self.step = 0
            episode_reward = 1

            while True:
                try:
                    action = self.rebalance(env.get_observation(True))
                    obs, reward, _, status = env.step(action)
                    episode_reward *= np.float64(reward)

                    self.step += 1

                    if visualize:
                        env.render()

                    if verbose:
                        print(">> step {0}/{1}, {2} % done, Cumulative Reward: {3}, ETC: {4}, Samples/s: {5:.04f}                   ".format(
                            self.step,
                            nb_max_episode_steps - env.obs_steps - 2,
                            int(100 * self.step / (nb_max_episode_steps - env.obs_steps - 2)),
                            episode_reward,
                            str(pd.to_timedelta((time() - t0) * ((nb_max_episode_steps - env.obs_steps - 2)
                                                                 - self.step), unit='s')),
                            1 / (time() - t0)
                        ), end="\r", flush=True)
                        t0 = time()

                    if status['OOD'] or self.step == nb_max_episode_steps:
                        return episode_reward

                    if status['Error']:
                        # e = status['Error']
                        # print("Env error:",
                        #       type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                        break

                except Exception as e:
                    print("Model Error:",
                          type(e).__name__ + ' in line ' + str(e.__traceback__.tb_lineno) + ': ' + str(e))
                    raise e

        except KeyboardInterrupt:
            print("\nKeyboard Interrupt: Stoping backtest\nElapsed steps: {0}/{1}, {2} % done.".format(self.step,
                                                                             nb_max_episode_steps,
                                                                             int(100 * self.step / nb_max_episode_steps)))


class RandomWalk(APrioriAgent):
    """
    Dummytrader that sample actions from a random process
    """
    def __repr__(self):
        return "RandomWalk"

    def __init__(self, random_process=None, activation='softmax', fiat="USDT"):
        """
        Initialization method
        :param env: Apocalipse driver instance
        :param random_process: Random process used to sample actions from
        :param activation: Portifolio activation function
        """
        super().__init__(fiat)

        self.random_process = random_process
        self.activation = activation

    def predict(self, obs):
        """
        Performs a single step on the environment
        """
        if self.random_process:
            if self.activation == 'softmax':
                return array_normalize(self.random_process.sample())
            elif self.activation == 'simplex':
                return self.simplex_proj(self.random_process.sample())
            else:
                return np.array(self.random_process.sample())
        else:
            if self.activation == 'softmax':
                return array_normalize(np.random.random(obs.columns.levels[0].shape[0]))
            elif self.activation == 'simplex':
                return self.simplex_proj(np.random.random(obs.columns.levels[0].shape[0]))
            else:
                return np.random.random(obs.columns.levels[0].shape[0])

    def rebalance(self, obs):
        return self.predict(obs)


class BuyAndHold(APrioriAgent):
    """
    Equally distribute cash at the first step and hold
    """
    def __repr__(self):
        return "BuyAndHold"

    def __init__(self, fiat="USDT"):
        super().__init__(fiat)

    def predict(self, obs):
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs - 1)
            return array_normalize(action)
        else:
            return self.get_portfolio_vector(obs)[:-1]

    def rebalance(self, obs):
        position = self.predict(obs)
        position.resize(obs.columns.levels[0].shape[0])
        position[-1] = self.get_portfolio_vector(obs)[-1]
        return position


class ConstantRebalance(APrioriAgent):
    """
    Equally distribute portfolio every step
    """
    def __repr__(self):
        return "ContantRebalance"

    def __init__(self, position=None, fiat="USDT"):
        super().__init__(fiat)
        if position:
            self.position = array_normalize(position)
        else:
            self.position = False

    def predict(self, obs):
        if not isinstance(self.position, np.ndarray):
            n_symbols = obs.columns.levels[0].shape[0]
            self.position = array_normalize(np.ones(n_symbols - 1))
            self.position = np.append(self.position, [0.0])

        return self.position

    def rebalance(self, obs):
        factor = self.predict(obs)
        return factor

    def set_params(self, **kwargs):
        self.position = np.append(array_normalize(np.array([kwargs[key]
                                            for key in kwargs]))[:-1], [0.0])


# Momentum
class Momentum(APrioriAgent):
    """
    Momentum trading agent
    """
    def __repr__(self):
        return "Momentum"

    def __init__(self, ma_span=[2, 3], std_span=3, weights=[1., 1.], mean_type='kama', sensitivity=0.1, rebalance=True,
                 activation=simplex_proj, fiat="USDT"):
        """
        :param mean_type: str: Mean type to use. It can be simple, exp or kama.
        """
        super().__init__(fiat=fiat)
        self.mean_type = mean_type
        self.ma_span = ma_span
        self.std_span = std_span
        self.weights = weights
        self.sensitivity = sensitivity
        self.activation = activation
        if rebalance:
            self.reb = -2
        else:
            self.reb = -1


    def get_ma(self, df):
        if self.mean_type == 'exp':
            for window in self.ma_span:
                df[str(window) + '_ma'] = df.open.ewm(span=window).mean()
        elif self.mean_type == 'kama':
            for window in self.ma_span:
                df[str(window) + '_ma'] = tl.KAMA(df.open.values, timeperiod=window)
        elif self.mean_type == 'simple':
            for window in self.ma_span:
                df[str(window) + '_ma'] = df.open.rolling(window).mean()
        else:
            raise TypeError("Wrong mean_type param")
        return df

    def predict(self, obs):
        """
        Performs a single step on the environment
        """
        try:
            obs = obs.astype(np.float64)
            factor = np.zeros(obs.columns.levels[0].shape[0], dtype=np.float64)
            for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
                df = obs.loc[:, symbol].copy()
                df = self.get_ma(df)

                p = (df['%d_ma' % self.ma_span[0]].iat[-1] - df['%d_ma' % self.ma_span[1]].iat[-1])

                d = (df['%d_ma' % self.ma_span[0]].iloc[-4:] - df['%d_ma' % self.ma_span[1]].iloc[-4:]).diff()

                factor[key] = self.weights[0] * (p + self.weights[1] * d.iat[-1]) / \
                              (df.open.iloc[-self.std_span:].std() + self.epsilon)
                              # (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon)


            return array_normalize(factor) + 1

        except TypeError as e:
            print("\nYou must fit the model or provide indicator parameters in order for the model to act.")
            raise e

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x <= eps
        and minimize distance to previous portfolio.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        x_mean = np.mean(x)
        portvar = np.dot(b, x)

        change = (abs(portvar - 1) + max(abs(x - 1))) / 2

        lam = np.clip((change - self.sensitivity) / (np.linalg.norm(x - x_mean) ** 2 + self.epsilon), 0.0, 1e6)

        # update portfolio
        b = b + lam * (x - x_mean)

        # Log values
        self.log['mean_pct_change_prediction'] = ((1 / x_mean) - 1) * 100
        self.log['portfolio_pct_change_prediction'] = ((1 / portvar) - 1) * 100

        # project it onto simplex
        return self.activation(b)

    def rebalance(self, obs):
        try:
            obs = obs.astype(np.float64)
            if self.step == 0:
                n_pairs = obs.columns.levels[0].shape[0]
                action = np.ones(n_pairs)
                action[-1] = 0
                return array_normalize(action)
            else:
                prev_posit = self.get_portfolio_vector(obs, index=self.reb)
                factor = self.predict(obs)
                return self.update(prev_posit, factor)

        except TypeError as e:
            print("\nYou must fit the model or provide indicator parameters in order for the model to act.")
            raise e

    def set_params(self, **kwargs):
        self.weights = [kwargs['alpha_v'], kwargs['alpha_a']]
        self.mean_type = kwargs['mean_type']
        self.ma_span = [int(kwargs['ma1']), int(kwargs['ma2'])]
        self.std_span = int(kwargs['std_span'])


class ONS(APrioriAgent):
    """
    Online Newton Step algorithm.
    Reference:
        A.Agarwal, E.Hazan, S.Kale, R.E.Schapire.
        Algorithms for Portfolio Management based on the Newton Method, 2006.
        http://machinelearning.wustl.edu/mlpapers/paper_files/icml2006_AgarwalHKS06.pdf
    """

    def __repr__(self):
        return "ONS"

    def __init__(self, delta=0.1, beta=2., eta=0., fiat="USDT", name=""):
        """
        :param delta, beta, eta: Model parameters. See paper.
        """
        super().__init__(fiat=fiat, name=name)
        self.delta = delta
        self.beta = beta
        self.eta = eta

    def predict(self, obs):
        price_relative = np.ones(obs.columns.levels[0].shape[0], dtype=np.float64)
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_relative[key] = np.float64(
                obs.get_value(obs.index[-1], (symbol, 'open')) / obs.get_value(obs.index[-2], (symbol, 'open')))
        return price_relative

    def rebalance(self, obs):
        if not self.step:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            self.A = np.mat(np.eye(n_pairs))
            self.b = np.mat(np.zeros(n_pairs)).T
            return array_normalize(action)
        else:
            prev_posit = self.get_portfolio_vector(obs, index=-1)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)

    def update(self, b, x):
        # calculate gradient
        grad = np.mat(x / np.dot(b, x)).T
        # update A
        self.A += grad * grad.T
        # update b
        self.b += (1 + 1. / self.beta) * grad

        # projection of p induced by norm A
        pp = self.projection_in_norm(self.delta * self.A.I * self.b, self.A)

        return pp * (1 - self.eta) + np.ones(len(x)) / float(len(x)) * self.eta

    def projection_in_norm(self, x, M):
        """
        Projection of x to simplex indiced by matrix M. Uses quadratic programming.
        """
        m = M.shape[0]

        # Constrains matrices
        P = opt.matrix(2 * M)
        q = opt.matrix(-2 * M * x)
        G = opt.matrix(-np.eye(m))
        h = opt.matrix(np.zeros((m, 1)))
        A = opt.matrix(np.ones((1, m)))
        b = opt.matrix(1.)

        # Solve using quadratic programming
        sol = opt.solvers.qp(P, q, G, h, A, b)
        return np.squeeze(sol['x'])

    def set_params(self, **kwargs):
        self.delta = kwargs['delta']
        self.beta = kwargs['beta']
        self.eta = kwargs['eta']


# Pattern trading
class HarmonicTrader(APrioriAgent):
    """
    Fibonacci harmonic pattern trader
    """

    def __repr__(self):
        return "HarmonicTrader"

    def __init__(self, peak_order=7, err_allowed=0.05, decay=0.99, activation=array_normalize, fiat="USDT"):
        """
        Fibonacci trader init method
        :param peak_order: Extreme finder movement magnitude threshold
        :param err_allowed: Pattern error margin to be accepted
        :param decay: float: Decay rate for portfolio selection. Between 0 and 1
        :param fiat: Fiat symbol to use in trading
        """
        super().__init__(fiat)
        self.err_allowed = err_allowed
        self.peak_order = peak_order
        self.alpha = [1., 1.]
        self.decay = decay
        self.activation = activation

    def find_extreme(self, obs):
        max_idx = argrelextrema(obs.open.values, np.greater, order=self.peak_order)[0]
        min_idx = argrelextrema(obs.open.values, np.less, order=self.peak_order)[0]
        extreme_idx = np.concatenate([max_idx, min_idx, [obs.shape[0] - 1]])
        extreme_idx.sort()
        return obs.open.iloc[extreme_idx]

    def calc_intervals(self, extremes):
        XA = extremes.iloc[-2] - extremes.iloc[-1]
        AB = extremes.iloc[-3] - extremes.iloc[-2]
        BC = extremes.iloc[-4] - extremes.iloc[-3]
        CD = extremes.iloc[-5] - extremes.iloc[-4]

        return XA, AB, BC, CD

    def find_pattern(self, obs, c1, c2, c3):
        try:
            XA, AB, BC, CD = self.calc_intervals(self.find_extreme(obs))

            # Gartley fibonacci pattern
            AB_range = np.array([c1[0] - self.err_allowed, c1[1] + self.err_allowed]) * abs(XA)
            BC_range = np.array([c2[0] - self.err_allowed, c2[1] + self.err_allowed]) * abs(AB)
            CD_range = np.array([c3[0] - self.err_allowed, c3[1] + self.err_allowed]) * abs(BC)

            if AB_range[0] < abs(AB) < AB_range[1] and \
                                    BC_range[0] < abs(BC) < BC_range[1] and \
                                    CD_range[0] < abs(CD) < CD_range[1]:
                if XA > 0 and AB < 0 and BC > 0 and CD < 0:
                    return 1
                elif XA < 0 and AB > 0 and BC < 0 and CD > 0:
                    return -1
                else:
                    return 0
            else:
                return 0
        except IndexError:
            return 0

    def is_gartley(self, obs):
        return self.find_pattern(obs, c1=(0.618, 0.618), c2=(0.382, 0.886), c3=(1.27, 1.618))

    def is_butterfly(self, obs):
        return self.find_pattern(obs, c1=(0.786, 0.786), c2=(0.382, 0.886), c3=(1.618, 2.618))

    def is_bat(self, obs):
        return self.find_pattern(obs, c1=(0.382, 0.5), c2=(0.382, 0.886), c3=(1.618, 2.618))

    def is_crab(self, obs):
        return self.find_pattern(obs, c1=(0.382, 0.618), c2=(0.382, 0.886), c3=(2.24, 3.618))

    def predict(self, obs):
        pairs = obs.columns.levels[0]
        action = np.zeros(pairs.shape[0] - 1)
        for i, pair in enumerate(pairs):
            if pair is not self.fiat:
                pattern = np.array([pattern(obs[pair]) for pattern in [self.is_gartley,
                                                                       self.is_butterfly,
                                                                       self.is_bat,
                                                                       self.is_crab]]).sum()

                action[i] = pattern

        return action

    def rebalance(self, obs):
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            port_vec = np.ones(n_pairs)
            port_vec[-1] = 0
        else:
            pairs = obs.columns.levels[0]
            prev_port = self.get_portfolio_vector(obs)
            action = self.predict(obs)
            port_vec = np.zeros(pairs.shape[0])
            for i in range(pairs.shape[0] - 1):
                if action[i] >= 0:
                    port_vec[i] = max(0.,
                                      (self.decay * prev_port[i] + (1 - self.decay)) + self.alpha[0] * action[
                                          i])
                else:
                    port_vec[i] = max(0.,
                                      (self.decay * prev_port[i] + (1 - self.decay)) + self.alpha[1] * action[
                                          i])

            port_vec[-1] = max(0, 1 - port_vec.sum())

        return self.activation(port_vec)

    def set_params(self, **kwargs):
        self.err_allowed = kwargs['err_allowed']
        self.peak_order = int(kwargs['peak_order'])
        self.decay = kwargs['decay']
        self.alpha = [kwargs['alpha_up'], kwargs['alpha_down']]


# Mean reversion
class PAMR(APrioriAgent):
    """
    Passive aggressive mean reversion strategy for portfolio selection.

    Reference:
        B. Li, P. Zhao, S. C.H. Hoi, and V. Gopalkrishnan.
        Pamr: Passive aggressive mean reversion strategy for portfolio selection, 2012.
        https://link.springer.com/content/pdf/10.1007%2Fs10994-012-5281-z.pdf
    """
    def __repr__(self):
        return "PAMR"

    def __init__(self, sensitivity=0.03, C=2444, variant="PAMR1", fiat="USDT", name=""):
        """
        :param sensitivity: float: Sensitivity parameter. Lower is more sensitive.
        :param C: float: Aggressiveness parameter. For PAMR1 and PAMR2 variants.
        :param variant: str: The variant of the proposed algorithm. It can be PAMR, PAMR1, PAMR2.
        :
        """
        super().__init__(fiat=fiat, name=name)
        self.sensitivity = sensitivity
        self.C = C
        self.variant = variant

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        price_relative = np.empty(obs.columns.levels[0].shape[0], dtype=np.float64)
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_relative[key] = np.float64(obs.get_value(obs.index[-2], (symbol, 'open')) /
                                             (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon))

        price_relative[-1] = 1

        return price_relative

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)
        else:
            prev_posit = self.get_portfolio_vector(obs, index=-2)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x <= eps
        and minimize distance to previous portfolio.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        # x_mean = np.mean(x)
        # if np.dot(b, x) >= 1:
        #     le = max(0., np.dot(b, x) - (1 + self.sensitivity))
        # else:
        #     le = max(0, (1 - self.sensitivity) - np.dot(b, x))

        x_mean = np.mean(x)
        portvar = np.dot(b, x)

        if portvar > 1 + self.sensitivity:
            le = portvar - (1 + self.sensitivity)
        # elif portvar < 1 - self.sensitivity:
        #     le = (1 - self.sensitivity) - portvar

        if self.variant == 'PAMR0':
            lam = le / (np.linalg.norm(x - x_mean) ** 2 + self.epsilon)
        elif self.variant == 'PAMR1':
            lam = min(self.C, le / (np.linalg.norm(x - x_mean) ** 2 + self.epsilon))
        elif self.variant == 'PAMR2':
            lam = le / (np.linalg.norm(x - x_mean) ** 2 + 0.5 / self.C + self.epsilon)
        else:
            raise TypeError("Bad variant param.")

        # limit lambda to avoid numerical problems
        lam = min(100000, lam)

        # update portfolio
        b = b + lam * (x - x_mean)

        # project it onto simplex
        return simplex_proj(b)

    def set_params(self, **kwargs):
        self.sensitivity = kwargs['sensitivity']
        if 'C' in kwargs:
            self.C = kwargs['C']
        self.variant = kwargs['variant']
        self.alpha = kwargs['alpha']


class OLMAR(APrioriAgent):
    """
        On-Line Portfolio Selection with Moving Average Reversio

        Reference:
            B. Li and S. C. H. Hoi.
            On-line portfolio selection with moving average reversion, 2012.
            http://icml.cc/2012/papers/168.pdf
        """

    def __repr__(self):
        return "OLMAR"

    def __init__(self, window=7, eps=0.02, smooth = 0.5, fiat="USDT", name=""):
        """
        :param window: integer: Lookback window size.
        :param eps: float: Threshold value for updating portfolio.
        """
        super().__init__(fiat=fiat, name=name)
        self.window = window
        self.eps = eps
        self.smooth = smooth

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        price_predict = np.empty(obs.columns.levels[0].shape[0] - 1, dtype=np.float64)
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_predict[key] = np.float64(obs[symbol].open.iloc[-self.window - 1:-1].mean() /
                                            (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon))
        return price_predict

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)
        else:
            prev_posit = self.get_portfolio_vector(obs, index=-2)
            price_predict = self.predict(obs)
            return self.update(prev_posit[:-1], price_predict)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x >= eps
        and minimize distance to previous weights.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        x_mean = np.mean(x)
        if np.dot(b, x) >= 1:
            lam = max(0., (np.dot(b, x) - 1 - self.eps) / (np.linalg.norm(x - x_mean) ** 2 + self.epsilon))
        else:
            lam = max(0, (1 - self.eps - np.dot(b, x)) / (np.linalg.norm(x - x_mean) ** 2 + self.epsilon))

        # limit lambda to avoid numerical problems
        lam = min(100000, lam)

        # update portfolio
        b = b + self.smooth * lam * (x - x_mean)

        # project it onto simplex
        return np.append(simplex_proj(b), [0])

    def set_params(self, **kwargs):
        self.eps = kwargs['eps']
        self.window = int(kwargs['window'])
        self.smooth = kwargs['smooth']


class STMR(APrioriAgent):
    """
    Short term mean reversion strategy for portfolio selection.

    Original algo by José Olímpio Mendes
    27/11/2017
    """
    def __repr__(self):
        return "STMR"

    def __init__(self, sensitivity=0.02, rebalance=True, activation=simplex_proj, fiat="USDT", name=""):
        """
        :param sensitivity: float: Sensitivity parameter. Lower is more sensitive.
        """
        super().__init__(fiat=fiat, name=name)
        self.sensitivity = sensitivity
        self.activation = activation
        if rebalance:
            self.reb = -2
        else:
            self.reb = -1

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        obs = obs.astype(np.float64)
        price_relative = np.empty(obs.columns.levels[0].shape[0], dtype=np.float64)
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_relative[key] = (obs.get_value(obs.index[-2], (symbol, 'open')) /
                                    (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon) - 1)

        price_relative[-1] = 0

        return price_relative

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)
        else:
            prev_posit = self.get_portfolio_vector(obs, index=self.reb)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x <= eps
        and minimize distance to previous portfolio.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        x_mean = np.mean(x)
        portvar = np.dot(b, x)

        change = abs((portvar + x[np.argmax(abs(x))]) / 2)

        lam = np.clip(safe_div(change - self.sensitivity, np.linalg.norm(x - x_mean) ** 2), 0.0, 1e6)

        # update portfolio
        b = b + lam * (x - x_mean)

        # project it onto simplex
        return self.activation(b)

    def set_params(self, **kwargs):
        self.sensitivity = kwargs['sensitivity']


class CWMR(APrioriAgent):
    """ Confidence weighted mean reversion.
    Reference:
        B. Li, S. C. H. Hoi, P.L. Zhao, and V. Gopalkrishnan.
        Confidence weighted mean reversion strategy for online portfolio selection, 2013.
        http://jmlr.org/proceedings/papers/v15/li11b/li11b.pdf
    """

    def __repr__(self):
        return "CWMR"

    def __init__(self, eps=-0.5, confidence=0.95, var=0, rebalance=True, fiat="USDT", name=""):
        """
        :param eps: Mean reversion threshold (expected return on current day must be lower
                    than this threshold). Recommended value is -0.5.
        :param confidence: Confidence parameter for profitable mean reversion portfolio.
                    Recommended value is 0.95.
        """
        super(CWMR, self).__init__(fiat=fiat, name=name)

        # input check
        if not (0 <= confidence <= 1):
            raise ValueError('confidence must be from interval [0,1]')
        if rebalance:
            self.reb = -2
        else:
            self.reb = -1
        self.eps = eps
        self.theta = scipy.stats.norm.ppf(confidence)
        self.var = var

    def predict(self, obs):
        """
        Performs prediction given environment observation
        """
        obs = obs.astype(np.float64)
        price_relative = np.empty(obs.columns.levels[0].shape[0], dtype=np.float64)
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_relative[key] = (obs.get_value(obs.index[-2], (symbol, 'open')) /
                                    (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon))

        price_relative[-1] = 1
        return price_relative

    def update(self, b, x):
        # initialize
        m = len(x)
        mu = np.matrix(b).T
        sigma = self.sigma
        theta = self.theta
        eps = self.eps
        x = np.matrix(x).T  # matrices are easier to manipulate

        # 4. Calculate the following variables
        M = mu.T * x
        V = x.T * sigma * x
        x_upper = sum(diag(sigma) * x) / trace(sigma)

        # 5. Update the portfolio distribution
        mu, sigma = self.calculate_change(x, x_upper, mu, sigma, M, V, theta, eps)

        # 6. Normalize mu and sigma
        mu = simplex_proj(mu)
        sigma = sigma / (m ** 2 * trace(sigma))
        """
        sigma(sigma < 1e-4*eye(m)) = 1e-4;
        """
        self.sigma = sigma

        return np.array(mu.T).ravel()

    def calculate_change(self, x, x_upper, mu, sigma, M, V, theta, eps):
        if not self.var:
            # lambda from equation 7
            foo = (V - x_upper * x.T * np.sum(sigma, axis=1)) / M ** 2 + V * theta ** 2 / 2.
            a = foo ** 2 - V ** 2 * theta ** 4 / 4
            b = 2 * (eps - log(M)) * foo
            c = (eps - log(M)) ** 2 - V * theta ** 2

            a, b, c = a[0, 0], b[0, 0], c[0, 0]

            lam = max(0,
                      (-b + sqrt(b ** 2 - 4 * a * c)) / (2. * a),
                      (-b - sqrt(b ** 2 - 4 * a * c)) / (2. * a))
            # bound it due to numerical problems
            lam = min(lam, 1E+7)

            # update mu and sigma
            U_sqroot = 0.5 * (-lam * theta * V + sqrt(lam ** 2 * theta ** 2 * V ** 2 + 4 * V))
            mu = mu - lam * sigma * (x - x_upper) / M
            sigma = inv(inv(sigma) + theta * lam / U_sqroot * diag(x) ** 2)
            """
            tmp_sigma = inv(inv(sigma) + theta*lam/U_sqroot*diag(xt)^2);
            % Don't update sigma if results are badly scaled.
            if all(~isnan(tmp_sigma(:)) & ~isinf(tmp_sigma(:)))
                sigma = tmp_sigma;
            end
            """

            return mu, sigma

        else:
            """ First variant of a CWMR outlined in original article. It is
            only approximation to the posted problem. """
            # lambda from equation 7
            foo = (V - x_upper * x.T * np.sum(sigma, axis=1)) / M ** 2
            a = 2 * theta * V * foo
            b = foo + 2 * theta * V * (eps - log(M))
            c = eps - log(M) - theta * V

            a, b, c = a[0, 0], b[0, 0], c[0, 0]

            lam = max(0,
                      (-b + sqrt(b ** 2 - 4 * a * c)) / (2. * a),
                      (-b - sqrt(b ** 2 - 4 * a * c)) / (2. * a))
            # bound it due to numerical problems
            lam = min(lam, 1E+7)

            # update mu and sigma
            mu = mu - lam * sigma * (x - x_upper) / M
            sigma = inv(inv(sigma) + 2 * lam * theta * diag(x) ** 2)
            """
            tmp_sigma = inv(inv(sigma) + theta*lam/U_sqroot*diag(xt)^2);
            % Don't update sigma if results are badly scaled.
            if all(~isnan(tmp_sigma(:)) & ~isinf(tmp_sigma(:)))
                sigma = tmp_sigma;
            end
            """

            return mu, sigma

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        n_pairs = obs.columns.levels[0].shape[0]
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=self.reb)
            price_relative = self.predict(obs)
            return self.update(prev_posit, price_relative)
        else:
            action = np.ones(n_pairs)
            action[-1] = 0
            self.sigma = np.matrix(np.eye(n_pairs) / n_pairs ** 2)
            return array_normalize(action)

    def set_params(self, **kwargs):
        self.eps = kwargs['eps']
        self.theta = scipy.stats.norm.ppf(kwargs['confidence'])


# Portfolio optimization
class TCO(APrioriAgent):
    """
    Transaction cost optimization for online portfolio selection

    Reference:
        B. Li and J. Wang
        http://ink.library.smu.edu.sg/cgi/viewcontent.cgi?article=4761&context=sis_research
    """
    def __repr__(self):
        return "TCO"

    def __init__(self, toff=0.1, predictor=None, fiat="USDT", name=""):
        """
        :param window: integer: Lookback window size.
        :param eps: float: Threshold value for updating portfolio.
        """
        super().__init__(fiat=fiat, name=name)
        self.toff = toff
        self.predictor = predictor

    def predict(self, obs):
        """
        Performs prediction given environment observation
        :param obs: pandas DataFrame: Environment observation
        """
        # price_predict = np.empty(obs.columns.levels[0].shape[0] - 1, dtype=np.float64)
        # for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
        #     price_predict[key] = np.float64(obs[symbol].open.iloc[-self.window:].mean() /
        #                                     (obs.get_value(obs.index[-1], (symbol, 'open')) + self.epsilon))
        return self.predictor.predict(obs)

    def rebalance(self, obs):
        """
        Performs portfolio rebalance within environment
        :param obs: pandas DataFrame: Environment observation
        :return: numpy array: Portfolio vector
        """
        if self.step == 0:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)
        else:
            prev_posit = self.get_portfolio_vector(obs, index=-1)
            price_prediction = self.predict(obs)
            return self.update(prev_posit, price_prediction)

    def update(self, b, x):
        """
        Update portfolio weights to satisfy constraint b * x >= eps
        and minimize distance to previous weights.
        :param b: numpy array: Last portfolio vector
        :param x: numpy array: Price movement prediction
        """
        vt = x / (np.dot(b, x) + self.epsilon)
        vt_mean = np.mean(vt)
        # update portfolio
        b = b + np.sign(vt - vt_mean) * np.clip(abs(vt - vt_mean) - self.toff, 0, np.inf)

        # project it onto simplex
        return simplex_proj(b)

    def set_params(self, **kwargs):
        self.toff = kwargs['toff']
        self.predictor.set_params(**kwargs)


class FactorTrader(APrioriAgent):
    """
    Compound factor trader
    """
    def __repr__(self):
        return "FactorTrader"

    def __init__(self, factors, std_window=3, std_weight=1., alpha=[1., 1.], activation=array_normalize, fiat="USDT"):
        super().__init__(fiat)
        assert isinstance(factors, list), "factors must be a list containing factor model instances"
        for factor in factors:
            assert isinstance(factor, APrioriAgent), "Factors must be APrioriAgent instances"
        self.factors = factors
        self.std_window = std_window
        self.std_weight = std_weight
        self.weights = np.ones(len(self.factors))
        self.alpha = alpha
        self.activation = activation

    def predict(self, obs):
        action = np.zeros(obs.columns.levels[0].shape[0] - 1, dtype=np.float64)
        for weight, factor in zip(self.weights, self.factors):
            action += weight * factor.predict(obs)
        return action

    def rebalance(self, obs):
        action = self.predict(obs)
        prev_port= self.get_portfolio_vector(obs)
        port_vec = np.zeros(prev_port.shape)
        for i, symbol in enumerate(obs.columns.levels[0]):
            if symbol is not self.fiat:
                if action[i] >= 0.:
                    port_vec[i] = max(0, prev_port[i] + self.alpha[0] * action[i] / \
                                              (self.std_weight * obs[symbol].open.rolling(self.std_window,
                                               min_periods=1, center=True).std().iat[-1] / obs.get_value(
                                               obs.index[-1], (symbol, 'open')) + self.epsilon))
                else:
                    port_vec[i] = max(0, prev_port[i] + self.alpha[1] * action[i] / \
                                              (self.std_weight * obs[symbol].open.rolling(self.std_window,
                                               min_periods=1, center=True).std().iat[-1] / obs.get_value(
                                               obs.index[-1], (symbol, 'open')) + self.epsilon))

        port_vec[-1] = max(0, 1 - port_vec.sum())

        return self.activation(port_vec)

    def set_params(self, **kwargs):
        self.std_window = int(kwargs['std_window'])
        self.std_weight = kwargs['std_weight']
        for i, factor in enumerate(self.factors):
            self.weights[i] = kwargs[str(factor) + '_weight']
        self.alpha = [kwargs['alpha_up'], kwargs['alpha_down']]

    def fit(self, env, nb_steps, batch_size, search_space, constrains=None, action_repetition=1, callbacks=None, verbose=1,
            visualize=False, nb_max_start_steps=0, start_step_policy=None, log_interval=10000,
            nb_max_episode_steps=None, n_workers=1):
        try:
            if verbose:
                print("Optimizing model for %d steps with batch size %d..." % (nb_steps, batch_size))

            i = 0
            t0 = time()
            env.training = True

            def find_hp(**kwargs):
                try:
                    nonlocal i, nb_steps, t0, env, nb_max_episode_steps

                    self.set_params(**kwargs)

                    batch_reward = []
                    for batch in range(batch_size):
                        # Reset env
                        env.reset_status()
                        env.reset(reset_dfs=True)
                        # run test on the main process
                        r = self.test(env,
                                        nb_episodes=1,
                                        action_repetition=action_repetition,
                                        callbacks=callbacks,
                                        visualize=visualize,
                                        nb_max_episode_steps=nb_max_episode_steps,
                                        nb_max_start_steps=nb_max_start_steps,
                                        start_step_policy=start_step_policy,
                                        verbose=False)

                        batch_reward.append(r)

                    i += 1
                    if verbose:
                        try:
                            print("Optimization step {0}/{1}, step reward: {2}, ETC: {3} ".format(i,
                                                                                nb_steps,
                                                                                sum(batch_reward) / batch_size,
                                                                                str(pd.to_timedelta((time() - t0) * (nb_steps - i), unit='s'))),
                                  end="\r")
                            t0 = time()
                        except TypeError:
                            print("\nOptimization aborted by the user.")
                            raise ot.api.fun.MaximumEvaluationsException(0)

                    return sum(batch_reward) / batch_size

                except KeyboardInterrupt:
                    print("\nOptimization aborted by the user.")
                    raise ot.api.fun.MaximumEvaluationsException(0)

            factor_weights = {}
            for factor in self.factors:
                factor_weights[str(factor) + "_weight"] = [0.00001, 1]

            opt_params, info, _ = ot.maximize(find_hp,
                                              num_evals=nb_steps,
                                              **search_space,
                                              **factor_weights
                                              )

            self.set_params(**opt_params)
            env.training = False
            return opt_params, info

        except KeyboardInterrupt:
            env.training = False
            print("\nOptimization interrupted by user.")
            return opt_params, info


class Anticor(APrioriAgent):
    """ Anticor (anti-correlation) is a heuristic portfolio selection algorithm.
    It adopts the consistency of positive lagged cross-correlation and negative
    autocorrelation to adjust the portfolio. Eventhough it has no known bounds and
    hence is not considered to be universal, it has very strong empirical results.
    Reference:
        A. Borodin, R. El-Yaniv, and V. Gogan.  Can we learn to beat the best stock, 2005.
        http://www.cs.technion.ac.il/~rani/el-yaniv-papers/BorodinEG03.pdf
    """

    def __repr__(self):
        return "Anticor"

    def __init__(self, window=30, fiat="USDT"):
        """
        :param window: Window parameter.
        """
        super().__init__(fiat=fiat)
        self.window = window

    def predict(self, obs):
        """

        :param obs:
        :return:
        """
        price_log1 = np.empty((self.window - 2, obs.columns.levels[0].shape[0] - 1), dtype='f')
        price_log2 = np.empty((self.window - 2, obs.columns.levels[0].shape[0] - 1), dtype='f')
        for key, symbol in enumerate([s for s in obs.columns.levels[0] if s is not self.fiat]):
            price_log1[:, key] = obs[symbol].open.iloc[-2 * self.window + 1:-self.window].rolling(2).apply(
                lambda x: np.log10(safe_div(x[-1], x[-2]))).dropna().values.T
            price_log2[:, key] = obs[symbol].open.iloc[-self.window + 1:].rolling(2).apply(
                lambda x: np.log10(safe_div(x[-1], x[-2]))).dropna().values.T
        return price_log1, price_log2

    def rebalance(self, obs):
        if self.step:
            prev_posit = self.get_portfolio_vector(obs, index=-1)[:-1]
            factor = self.predict(obs)
            return self.update(prev_posit, *factor)
        else:
            n_pairs = obs.columns.levels[0].shape[0]
            action = np.ones(n_pairs)
            action[-1] = 0
            return array_normalize(action)

    @staticmethod
    def zero_to_inf(vec):
        return np.vectorize(lambda x: np.inf if np.allclose(x, [0.0]) else x)(vec)

    def update(self, b, lx1, lx2):
        mean2 = lx2.mean(axis=0)
        std1 = self.zero_to_inf(lx1.std(axis=0))
        std2 = self.zero_to_inf(lx2.std(axis=0))

        corr = np.matmul(((lx1 - lx1.mean(axis=0)) / std1).T, (lx2 - mean2) / std2)
        claim = np.zeros_like(corr)

        for i in range(corr.shape[0]):
            for j in range(corr.shape[1]):
                if i == j: continue
                else:
                    if mean2[i] > mean2[j] and corr[i, j] > 0:
                        # Correlation matrix
                        claim[i, j] += corr[i, j]
                        # autocorrelation
                        if corr[i, i] < 0:
                            claim[i, j] += abs(corr[i, i])
                        if corr[j, j] < 0:
                            claim[i, j] += abs(corr[j, j])

        # calculate transfer
        transfer = claim * 0.
        for i in range(corr.shape[0]):
            total_claim = sum(claim[i, :])
            if total_claim != 0:
                transfer[i, :] = b[i] * safe_div(claim[i, :], total_claim)

        b += + np.sum(transfer, axis=0) - np.sum(transfer, axis=1)

        return np.append(simplex_proj(b), [0.0])

    def set_params(self, **kwargs):
        self.window = int(kwargs['window'])