# TODO: package, make imports smaller
from typing import OrderedDict
from recipe.i6_experiments.users.schupp.hybrid_hmm_nn.args import setup_god as god
from recipe.i6_experiments.users.schupp.hybrid_hmm_nn.args import conformer_config_returnn_baseargs as experiment_config_args
from recipe.i6_experiments.users.schupp.hybrid_hmm_nn.args import conformer_returnn_dict_network_generator

from sisyphus import gs
import copy

import inspect

OUTPUT_PATH = "conformer/stochastic_depth/"
gs.ALIAS_AND_OUTPUT_SUBDIR = OUTPUT_PATH

def main():
  sd_only_conv()
  sd_only_ff()

def sd_only_conv():

  NAME = "baseline+bs-7254+sd-conv"
  config_args = copy.deepcopy(experiment_config_args.config_baseline_00)
  config_args["batch_size"] = 7254

  from recipe.i6_experiments.users.schupp.hybrid_hmm_nn.args.conv_mod_versions import make_conv_mod_003_sd

  conv_args = copy.deepcopy(experiment_config_args.conv_default_args_00)
  conv_args.update(OrderedDict(
    survival_prob = 0.5 # For all conv layers, see make_conformer_02 for variations on every layer
  ))

  conformer_args = copy.deepcopy(experiment_config_args.conformer_default_args_00)

  god.create_experiment_world_001(
    name=NAME,
    output_path=OUTPUT_PATH,
    config_base_args=config_args,
    extra_returnn_net_creation_args = OrderedDict(
      recoursion_depth=8000, # Maybe a little high but who bothers
    ),
    conformer_create_func=conformer_returnn_dict_network_generator.make_conformer_00,
    conformer_func_args=OrderedDict(
      # sampling args
      sampling_func_args = experiment_config_args.sampling_default_args_00,

      # Feed forward args, both the same by default
      ff1_func_args = experiment_config_args.ff_default_args_00,
      ff2_func_args = experiment_config_args.ff_default_args_00,

      # Self attention args
      sa_func_args = experiment_config_args.sa_default_args_00,

      # Conv mod args
      conformer_self_conv_func = make_conv_mod_003_sd, # TODO: currently here is batchnorm disabled, cause of weird train error with it
      conv_func_args = conv_args,

      # Shared model args
      shared_model_args = experiment_config_args.shared_network_args_00,

      # Conformer args
      **conformer_args,

      ),
      returnn_train_post_config=experiment_config_args.returnn_train_post_config_00,
      returnn_rasr_args_defaults=experiment_config_args.returnn_rasr_args_defaults_00,

      #test_construction = True
  )

def sd_only_ff():

  NAME = "baseline+bs-7254+sd-ff"
  config_args = copy.deepcopy(experiment_config_args.config_baseline_00)
  config_args["batch_size"] = 7254

  from recipe.i6_experiments.users.schupp.hybrid_hmm_nn.args.ff_mod_versions import make_ff_mod_002_sd

  conv_args = copy.deepcopy(experiment_config_args.conv_default_args_00)

  ff_args = copy.deepcopy(experiment_config_args.ff_default_args_00)
  ff_args.update(OrderedDict(
    survival_prob = 0.5,
  ))

  conformer_args = copy.deepcopy(experiment_config_args.conformer_default_args_00)

  god.create_experiment_world_001(
    name=NAME,
    output_path=OUTPUT_PATH,
    config_base_args=config_args,
    extra_returnn_net_creation_args = OrderedDict(
      recoursion_depth=8000, # Maybe a little high but who bothers
    ),
    conformer_create_func=conformer_returnn_dict_network_generator.make_conformer_00,
    conformer_func_args=OrderedDict(
      # sampling args
      sampling_func_args = experiment_config_args.sampling_default_args_00,

      # Feed forward args, both the same by default
      conformer_ff1_func = make_ff_mod_002_sd, # Version with sd on both!
      conformer_ff2_func = make_ff_mod_002_sd,

      ff1_func_args = ff_args,
      ff2_func_args = ff_args,


      # Self attention args
      sa_func_args = experiment_config_args.sa_default_args_00,

      # Conv mod args
      conv_func_args = conv_args,

      # Shared model args
      shared_model_args = experiment_config_args.shared_network_args_00,

      # Conformer args
      **conformer_args,

      ),
      returnn_train_post_config=experiment_config_args.returnn_train_post_config_00,
      returnn_rasr_args_defaults=experiment_config_args.returnn_rasr_args_defaults_00,

      #test_construction = True
  )