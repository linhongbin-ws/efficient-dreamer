import collections
import logging
import os
import pathlib
import re
import sys
import warnings
import tracemalloc
import linecache

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
logging.getLogger().setLevel('ERROR')
warnings.filterwarnings('ignore', '.*box bound precision lowered.*')

sys.path.append(str(pathlib.Path(__file__).parent))
sys.path.append(str(pathlib.Path(__file__).parent.parent))

import numpy as np
import ruamel.yaml as yaml

import agent
import common

from common import Config
from common import GymWrapper
from common import RenderImage
from common import TerminalOutput
from common import JSONLOutput
from common import TensorBoardOutput
from tqdm import tqdm
import shutil 

# configs = yaml.safe_load(
#     (pathlib.Path(__file__).parent / 'configs.yaml').read_text())
# defaults = common.Config(configs.pop('defaults'))

import tensorflow as tf
for gpu in tf.config.experimental.list_physical_devices('GPU'):
    tf.config.experimental.set_memory_growth(gpu, True)

    
def train(env, config, time_limit, outputs=None, is_pure_train=False, is_pure_datagen=False, skip_gym_wrap=False, bc_dir=''):
  assert not (is_pure_train and is_pure_datagen)
  tf.config.experimental_run_functions_eagerly(not config.jit)
  logdir = pathlib.Path(config.logdir).expanduser()
  logdir.mkdir(parents=True, exist_ok=True)
  offlinelogdir = logdir / 'offline' if is_pure_train else logdir
  offlinelogdir.mkdir(parents=True, exist_ok=True)
  config.save(logdir / 'config.yaml')
  print(config, '\n')
  print('Logdir', logdir)

  if bc_dir is not '':
    oracle_dir = logdir / 'train_episodes' / 'oracle'
    if oracle_dir.exists():
      shutil.rmtree(str(oracle_dir))
    _ = shutil.copytree(bc_dir, oracle_dir) 
    if config.bc:
      bc_replay = common.Replay(oracle_dir, **config.replay)
      bc_dataset = iter(bc_replay.dataset(**config.dataset))
    else:
      bc_dataset = None
  else:
    bc_dataset = None

  train_replay = common.Replay(logdir / 'train_episodes', **config.replay)
  eval_replay = common.Replay(logdir / 'eval_episodes', **dict(
    capacity=config.replay.capacity // 10,
    minlen=config.dataset.length,
    maxlen=config.dataset.length))
  step = common.Counter(0 if is_pure_train else train_replay.stats['total_steps'])
  outputs = outputs or [
      common.TerminalOutput(),
      common.JSONLOutput(str(offlinelogdir)),
      common.TensorBoardOutput(str(offlinelogdir)),
  ]

  logger = common.Logger(step, outputs, multiplier=config.action_repeat)
  metrics = collections.defaultdict(list)

  should_train = common.Every(config.train_every)
  should_log = common.Every(config.log_every)
  should_video_train = common.Every(config.eval_video_every)
  should_video_eval = common.Every(config.eval_video_every)
  should_expl = common.Until(config.expl_until)


  if not skip_gym_wrap:
    env = common.GymWrapper(env)
    env = common.ResizeImage(env)
  if hasattr(env.act_space['action'], 'n'):
    env = common.OneHotAction(env)
  else:
    env = common.NormalizeAction(env)
  env = common.TimeLimit(env, time_limit)

  if not is_pure_train:
    # prefill agent
    prefill_replay = common.Replay(logdir / 'train_episodes' / config.prefill_agent, **config.replay)
    prefill_driver = common.Driver([env])
    prefill_driver.on_step(lambda tran, worker: step.increment())
    prefill_driver.on_step(prefill_replay.add_step)
    prefill_driver.on_reset(prefill_replay.add_step)

    prefill = max(0, config.prefill - prefill_replay.stats['total_steps'])
    if prefill:
      print(f'Prefill dataset ({prefill} steps).')
      if config.prefill_agent == 'random':
        prefill_agent = common.RandomAgent(env.act_space)
      elif config.prefill_agent == 'oracle':
        prefill_agent = common.OracleAgent(env.act_space, env=env)
      prefill_driver(prefill_agent, steps=prefill, episodes=1)
      prefill_driver.reset()
    
      del prefill_replay
      del prefill_driver
      
  train_replay = common.Replay(logdir / 'train_episodes', **config.replay)
  eval_replay = common.Replay(logdir / 'eval_episodes', **dict(
    capacity=config.replay.capacity // 10,
    minlen=config.dataset.length,
    maxlen=config.dataset.length))

  
  step = common.Counter(train_replay.stats['total_steps'])
  logger = common.Logger(step, outputs, multiplier=config.action_repeat)
  def per_episode(ep, mode):
    length = len(ep['reward']) - 1
    score = float(ep['reward'].astype(np.float64).sum())
    print(f'{mode.title()} episode has {length} steps, return {score:.1f}.')
    logger.scalar(f'{mode}_return', score)
    logger.scalar(f'{mode}_length', length)
    for key, value in ep.items():
      if re.match(config.log_keys_sum, key):
        logger.scalar(f'sum_{mode}_{key}', ep[key].sum())
      if re.match(config.log_keys_mean, key):
        logger.scalar(f'mean_{mode}_{key}', ep[key].mean())
      if re.match(config.log_keys_max, key):
        logger.scalar(f'max_{mode}_{key}', ep[key].max(0).mean())
    should = {'train': should_video_train, 'eval': should_video_eval}[mode]
    if should(step):
      for key in config.log_keys_video:
        logger.video(f'{mode}_policy_{key}', ep[key])
    replay = dict(train=train_replay, eval=eval_replay)[mode]
    logger.add(replay.stats, prefix=mode)
    logger.write()

  # agent

  train_driver = common.Driver([env])
  train_driver.on_episode(lambda ep: per_episode(ep, mode='train'))
  train_driver.on_step(lambda tran, worker: step.increment())
  train_driver.on_step(train_replay.add_step)
  train_driver.on_reset(train_replay.add_step)
  
  eval_driver = common.Driver([env])
  eval_driver.on_episode(lambda ep: per_episode(ep, mode='eval'))
  eval_driver.on_episode(eval_replay.add_episode)
  
  prefill_eval_agent = common.RandomAgent(env.act_space)
  eval_driver(prefill_eval_agent, episodes=3)

  while True:
    try:
      train_dataset = iter(train_replay.dataset(**config.dataset))
      next(train_dataset)
    except Exception as e:
      print("encounter error:")
      print(e)
      print("fill 1 eps training eps...")
      random_agnt = common.RandomAgent(env.act_space)
      train_driver(random_agnt, episodes=1)
    else:
      break
  while True:
    try:
      eval_dataset = iter(eval_replay.dataset(**config.dataset))
      next(eval_dataset)
    except:
      random_agnt = common.RandomAgent(env.act_space)
      eval_driver(random_agnt, episodes=1)
    else:
      break

  print('Create agent.')
  agnt = agent.Agent(config, env.obs_space, env.act_space, step, env=env)


      

  bc_func = lambda dataset: next(dataset) if dataset is not None else None
  train_agent = common.CarryOverState(agnt.train, is_bc=bc_dataset is not None)
  train_agent(next(train_dataset), bc_func(bc_dataset))
  agnt.load_sep(logdir)
  if is_pure_train:
    step.value =  agnt.tfstep.numpy()
  train_policy = lambda *args: agnt.policy(
      *args, mode='explore' if should_expl(step) else 'train')
  eval_policy = lambda *args: agnt.policy(*args, mode='eval')

  def train_step(tran, worker):
    # print(step.value)
    if should_train(step):
      if config.train_steps > 20:
        pbar = tqdm(range(config.train_steps))
      else:
        pbar = range(config.train_steps)
      for _ in pbar:
        mets = train_agent(next(train_dataset), bc_func(bc_dataset))
        [metrics[key].append(value) for key, value in mets.items()]
        # des_str = f"image: {mets['image_c0_loss'].numpy():.5e} {mets['image_c1_loss'].numpy():.5e}  {mets['image_c2_loss'].numpy():.5e} actor: {mets['actor_pure_loss'].numpy():.4f} critic: {mets['critic_loss'].numpy():.4f} critic grad {mets['critic_grad_norm'].numpy():.4f}"
        des_str = ""
        if bc_dataset is not None and config.bc_loss:
          des_str = des_str + f"bc: {mets['actor_bc_loss'].numpy():.4f}"
        # if bc_dataset is not None and config.bc_data_agent_retrain: 
        #   des_str = des_str + \
        #       f" [retrain] actor: {mets['bc_retrain_actor_pure_loss'].numpy():.4f} critic: {mets['bc_retrain_critic_loss'].numpy():.4f} bc: {mets['bc_retrain_actor_bc_loss'].numpy():.4f} critic grad {mets['bc_retrain_critic_grad_norm'].numpy():.4f}"
          # des_str = des_str + f"bc_w: {mets['bc_grad_weight']:.2f} " 
        if isinstance(pbar, tqdm):
          pbar.set_description(des_str)
    if should_log(step):
      for name, values in metrics.items():
        logger.scalar(name, np.array(values, np.float64).mean())
        metrics[name].clear()
      if should_video_train(step):
        logger.add(agnt.report(next(train_dataset)), prefix='train')
        if bc_dataset is not None:
          bc_report = agnt.report(next(bc_dataset))
          logger.add({'bc_report_'+k: v for k,v in bc_report.items()} , prefix='train')
      logger.write(fps=True)
      agnt.save_sep(logdir)
      print("save param")
  train_driver.on_step(train_step)
  
  
  while step < config.steps:
    print("===========================================")
    print("Evaluate phase")
    eval_driver.reset()
    eval_driver(eval_policy, episodes=config.eval_eps)
    if should_video_eval(step):
      logger.add(agnt.report(next(eval_dataset)), prefix='eval')

    print("===========================================")
    print("Train phase...")
    train_driver.reset()
    train_driver(train_policy, steps=config.eval_every)
  
    logger.write(fps=True)
