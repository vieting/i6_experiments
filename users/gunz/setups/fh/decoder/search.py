__all__ = ["DecodingTensorMap", "FHDecoder"]

import copy
from dataclasses import dataclass
import typing

import i6_core.recognition as recog
import i6_core.rasr as rasr
import i6_core.am as am
import i6_core.mm as mm

from sisyphus import tk
from sisyphus.delayed_ops import Delayed

from ...common.decoder.rtf import ExtractSearchStatisticsJob
from ..factored import LabelInfo, PhoneticContext
from ..rust_scorer import RecompileTfGraphJob
from .config import PosteriorScales, PriorInfo, SearchParameters
from .scorer import FactoredHybridFeatureScorer


class DecodingTensorMap(typing.TypedDict, total=False):
    """Map of tensor names used during decoding."""

    in_classes: str
    """Name of the input tensor carrying the classes."""

    in_encoder_output: str
    """
    Name of input tensor for feeding in the previously obtained encoder output while
    processing the state posteriors.

    This can be different from `out_encoder_output` because the tensor name for feeding
    in the intermediate data for computing the output softmaxes can be different from
    the tensor name where the encoder-output is provided.
    """

    in_delta_encoder_output: str
    """
    Name of input tensor for feeding in the previously obtained delta encoder output.

    See `in_encoder_output` for further explanation and why this can be different
    from `out_delta_encoder_output`.
    """

    in_data: str
    """Name of the input tensor carrying the audio features."""

    in_seq_length: str
    """Tensor name of the tensor where the feature length is fed in (as a dimension)."""

    out_encoder_output: str
    """Name of output tensor carrying the raw encoder output (before any softmax)."""

    out_delta_encoder_output: str
    """Name of the output tensor carrying the raw delta encoder output (before any softmax)."""

    out_left_context: str
    """Tensor name of the softmax for the left context."""

    out_right_context: str
    """Tensor name of the softmax for the right context."""

    out_center_state: str
    """Tensor name of the softmax for the center state."""

    out_delta: str
    """Tensor name of the softmax for the delta output."""


def default_tensor_map() -> DecodingTensorMap:
    return {
        "in_classes": "extern_data/placeholders/classes/classes",
        "in_data": "extern_data/placeholders/data/data",
        "in_seq_length": "extern_data/placeholders/data/data_dim0_size",
        "in_delta_encoder_output": "delta-ce/output_batch_major",
        "in_encoder_output": "encoder-output/output_batch_major",
        "out_encoder_output": "encoder-output/output_batch_major",
        "out_delta_encoder_output": "deltaEncoder-output/output_batch_major",
        "out_left_context": "left-output/output_batch_major",
        "out_right_context": "right-output/output_batch_major",
        "out_center_state": "center-output/output_batch_major",
        "out_delta": "delta-ce/output_batch_major",
    }


@dataclass
class RecognitionJobs:
    lat2ctm: recog.LatticeToCtmJob
    sclite: recog.ScliteJob
    search: recog.AdvancedTreeSearchJob
    search_stats: typing.Optional[ExtractSearchStatisticsJob]


def get_feature_scorer(
    context_type: PhoneticContext,
    label_info: LabelInfo,
    feature_scorer_config,
    mixtures,
    silence_id: int,
    prior_info: PriorInfo,
    num_label_contexts: int,
    num_states_per_phone: int,
    num_encoder_output: int,
    loop_scale: float,
    forward_scale: float,
    silence_loop_penalty: float,
    silence_forward_penalty: float,
    posterior_scales: typing.Optional[PosteriorScales] = None,
    use_estimated_tdps=False,
    state_dependent_tdp_file: typing.Optional[typing.Union[str, tk.Path]] = None,
    is_min_duration=False,
    is_multi_encoder_output=False,
):
    assert (
        prior_info.center_state_prior is not None
        and prior_info.center_state_prior.file is not None
    )
    if not prior_info.center_state_prior.scale:
        print(f"center state prior scale is unset or zero, possible bug")
    if context_type.is_diphone():
        assert (
            prior_info.left_context_prior is not None
            and prior_info.left_context_prior.file is not None
        )
        if not prior_info.left_context_prior.scale:
            print(f"left context prior scale is unset or zero, possible bug")
    if context_type.is_triphone():
        assert (
            prior_info.right_context_prior is not None
            and prior_info.right_context_prior.file is not None
        )
        if not prior_info.right_context_prior.scale:
            print(f"right context prior scale is unset or zero, possible bug")

    return FactoredHybridFeatureScorer(
        feature_scorer_config,
        prior_mixtures=mixtures,
        context_type=context_type.value,
        prior_info=prior_info,
        num_states_per_phone=num_states_per_phone,
        num_label_contexts=num_label_contexts,
        silence_id=silence_id,
        num_encoder_output=num_encoder_output,
        posterior_scales=posterior_scales,
        is_multi_encoder_output=is_multi_encoder_output,
        loop_scale=loop_scale,
        forward_scale=forward_scale,
        silence_loop_penalty=silence_loop_penalty,
        silence_forward_penalty=silence_forward_penalty,
        use_estimated_tdps=use_estimated_tdps,
        state_dependent_tdp_file=state_dependent_tdp_file,
        is_min_duration=is_min_duration,
        use_word_end_classes=label_info.use_word_end_classes,
        use_boundary_classes=label_info.use_boundary_classes,
    )


def round2(num: float):
    return round(num, 2)


class FHDecoder:
    def __init__(
        self,
        name: str,
        search_crp,
        context_type: PhoneticContext,
        feature_path: typing.Union[str, tk.Path],
        model_path: typing.Union[str, tk.Path],
        graph: typing.Union[str, tk.Path],
        mixtures,
        eval_files,
        tf_library: typing.Optional[typing.Union[str, tk.Path]] = None,
        gpu=True,
        tensor_map: typing.Optional[typing.Union[dict, DecodingTensorMap]] = None,
        is_multi_encoder_output=False,
        silence_id=40,
        recompile_graph_for_feature_scorer=False,
        in_graph_acoustic_scoring=False,
    ):
        assert not (recompile_graph_for_feature_scorer and in_graph_acoustic_scoring)

        self.name = name
        self.search_crp = copy.deepcopy(search_crp)  # s.crp["dev_magic"]
        self.context_type = context_type  # PhoneticContext.value
        self.model_path = model_path
        self.graph = graph
        self.mixtures = mixtures
        self.is_multi_encoder_output = is_multi_encoder_output
        self.tdp = {}
        self.silence_id = silence_id

        self.tensor_map = default_tensor_map()
        if tensor_map is not None:
            self.tensor_map.update(tensor_map)

        self.eval_files = eval_files  # ctm file as ref

        self.bellman_post_config = False
        self.gpu = gpu
        self.library_path = (
            tf_library
            if tf_library is not None
            else "/work/asr4/raissi/ms-thesis-setups/lm-sa-swb/dependencies/binaries/recognition/NativeLstm2.so"
        )
        self.recompile_graph_for_feature_scorer = recompile_graph_for_feature_scorer
        self.in_graph_acoustic_scoring = in_graph_acoustic_scoring

        # LM attributes
        self.tfrnn_lms = {}

        # setting other attributes
        self.set_tf_fs_flow(feature_path, model_path, graph)
        self.set_fs_tf_config()

    def get_search_params(
        self,
        beam: float,
        beam_limit: int,
        we_pruning=0.5,
        we_pruning_limit=10000,
        lm_state_pruning=None,
        is_count_based=False,
    ):
        sp = {
            "beam-pruning": beam,
            "beam-pruning-limit": beam_limit,
            "word-end-pruning": we_pruning,
            "word-end-pruning-limit": we_pruning_limit,
        }
        if is_count_based:
            return sp
        if lm_state_pruning is not None:
            sp["lm-state-pruning"] = lm_state_pruning
        return sp

    def get_requirements(self, beam: float, is_lstm=False):
        # under 27 is short queue
        rtf = 15

        if not self.gpu:
            rtf *= 4

        if self.context_type not in [
            PhoneticContext.monophone,
            PhoneticContext.diphone,
        ]:
            rtf += 5

        if beam > 17:
            rtf += 10

        if is_lstm:
            rtf += 20
            mem = 16.0
            if "eval" in self.name:
                rtf *= 2
        else:
            mem = 8

        return {"rtf": rtf, "mem": mem}

    def get_lookahead_options(scale=1.0, hlimit=-1, clow=0, chigh=500):
        lmla_options = {
            "scale": scale,
            "history_limit": hlimit,
            "cache_low": clow,
            "cache_high": chigh,
        }
        return lmla_options

    def set_tf_fs_flow(self, feature_path, model_path, graph):
        tf_feature_flow = rasr.FlowNetwork()
        base_mapping = tf_feature_flow.add_net(feature_path)
        if self.is_multi_encoder_output:
            tf_flow = self.get_tf_flow_delta(model_path, graph)
        else:
            tf_flow = self.get_tf_flow(model_path, graph)

        tf_mapping = tf_feature_flow.add_net(tf_flow)

        tf_feature_flow.interconnect_inputs(feature_path, base_mapping)
        tf_feature_flow.interconnect(
            feature_path,
            base_mapping,
            tf_flow,
            tf_mapping,
            {"features": "input-features"},
        )
        tf_feature_flow.interconnect_outputs(tf_flow, tf_mapping)

        self.featureScorerFlow = tf_feature_flow

    def get_tf_flow(self, model_path, graph):
        tf_flow = rasr.FlowNetwork()
        tf_flow.add_input("input-features")
        tf_flow.add_output("features")
        tf_flow.add_param("id")
        tf_fwd = tf_flow.add_node("tensorflow-forward", "tf-fwd", {"id": "$(id)"})
        tf_flow.link("network:input-features", tf_fwd + ":features")
        tf_flow.link(tf_fwd + ":encoder-output", "network:features")

        tf_flow.config = rasr.RasrConfig()

        tf_flow.config[tf_fwd].input_map.info_0.param_name = "features"
        tf_flow.config[tf_fwd].input_map.info_0.tensor_name = self.tensor_map["in_data"]
        tf_flow.config[
            tf_fwd
        ].input_map.info_0.seq_length_tensor_name = self.tensor_map["in_seq_length"]

        tf_flow.config[tf_fwd].output_map.info_0.param_name = "encoder-output"
        tf_flow.config[tf_fwd].output_map.info_0.tensor_name = self.tensor_map[
            "out_encoder_output"
        ]

        tf_flow.config[tf_fwd].loader.type = "meta"
        tf_flow.config[tf_fwd].loader.meta_graph_file = graph
        tf_flow.config[tf_fwd].loader.saved_model_file = model_path
        tf_flow.config[tf_fwd].loader.required_libraries = self.library_path

        return tf_flow

    def get_tf_flow_delta(self, model_path, graph):
        tf_flow = rasr.FlowNetwork()
        tf_flow.add_input("input-features")
        tf_flow.add_output("features")
        tf_flow.add_param("id")
        tf_fwd = tf_flow.add_node("tensorflow-forward", "tf-fwd", {"id": "$(id)"})
        tf_flow.link("network:input-features", tf_fwd + ":features")

        concat = tf_flow.add_node(
            "generic-vector-f32-concat",
            "concatenation",
            {"check-same-length": True, "timestamp-port": "feature-1"},
        )

        tf_flow.link(tf_fwd + ":encoder-output", "%s:%s" % (concat, "feature-1"))
        tf_flow.link(tf_fwd + ":deltaEncoder-output", "%s:%s" % (concat, "feature-2"))

        tf_flow.link(concat, "network:features")

        tf_flow.config = rasr.RasrConfig()

        tf_flow.config[tf_fwd].input_map.info_0.param_name = "features"
        tf_flow.config[tf_fwd].input_map.info_0.tensor_name = self.tensor_map["in_data"]
        tf_flow.config[
            tf_fwd
        ].input_map.info_0.seq_length_tensor_name = self.tensor_map["in_seq_length"]

        tf_flow.config[tf_fwd].output_map.info_0.param_name = "encoder-output"
        tf_flow.config[tf_fwd].output_map.info_0.tensor_name = self.tensor_map[
            "out_encoder_output"
        ]

        tf_flow.config[tf_fwd].output_map.info_1.param_name = "deltaEncoder-output"
        tf_flow.config[tf_fwd].output_map.info_1.tensor_name = self.tensor_map[
            "out_delta_encoder_output"
        ]

        tf_flow.config[tf_fwd].loader.type = "meta"
        tf_flow.config[tf_fwd].loader.meta_graph_file = graph
        tf_flow.config[tf_fwd].loader.saved_model_file = model_path
        tf_flow.config[tf_fwd].loader.required_libraries = self.library_path

        return tf_flow

    def set_fs_tf_config(self):
        fs_tf_config = rasr.RasrConfig()
        fs_tf_config.loader = self.featureScorerFlow.config["tf-fwd"]["loader"]

        # dirty hack for the Rust feature scorer
        if self.recompile_graph_for_feature_scorer:
            fs_tf_config.loader.meta_graph_file = RecompileTfGraphJob(
                meta_graph_file=fs_tf_config.loader.meta_graph_file
            ).out_graph
        elif self.in_graph_acoustic_scoring:
            raise NotImplementedError("in-graph scoring is being reworked")

            fs_tf_config.loader.meta_graph_file = AddInGraphScorerJob(
                graph=fs_tf_config.loader.meta_graph_file,
                n_contexts=42,
                tensor_map=self.tensor_map,
            ).out_graph

        del fs_tf_config.input_map

        # input is the same for each model, since the label embeddings are calculated from the dense label identity
        fs_tf_config.input_map.info_0.param_name = "encoder-output"
        fs_tf_config.input_map.info_0.tensor_name = self.tensor_map["in_encoder_output"]

        # monophone does not have any context
        if self.context_type != PhoneticContext.monophone:
            fs_tf_config.input_map.info_1.param_name = "dense-classes"
            fs_tf_config.input_map.info_1.tensor_name = self.tensor_map["in_classes"]

        if self.context_type in [
            PhoneticContext.monophone,
            PhoneticContext.mono_state_transition,
        ]:
            fs_tf_config.output_map.info_0.param_name = "center-state-posteriors"
            fs_tf_config.output_map.info_0.tensor_name = self.tensor_map[
                "out_center_state"
            ]
            if self.context_type == PhoneticContext.mono_state_transition:
                # add the delta outputs
                fs_tf_config.output_map.info_1.param_name = "delta-posteriors"
                fs_tf_config.output_map.info_1.tensor_name = self.tensor_map[
                    "out_delta"
                ]

        if self.context_type in [
            PhoneticContext.diphone,
            PhoneticContext.diphone_state_transition,
        ]:
            fs_tf_config.output_map.info_0.param_name = "center-state-posteriors"
            fs_tf_config.output_map.info_0.tensor_name = self.tensor_map[
                "out_center_state"
            ]
            fs_tf_config.output_map.info_1.param_name = "left-context-posteriors"
            fs_tf_config.output_map.info_1.tensor_name = self.tensor_map[
                "out_left_context"
            ]
            if self.context_type == PhoneticContext.diphone_state_transition:
                fs_tf_config.output_map.info_2.param_name = "delta-posteriors"
                fs_tf_config.output_map.info_2.tensor_name = self.tensor_map[
                    "out_delta"
                ]

        if self.context_type == PhoneticContext.triphone_symmetric:
            fs_tf_config.output_map.info_0.param_name = "center-state-posteriors"
            fs_tf_config.output_map.info_0.tensor_name = self.tensor_map[
                "out_center_state"
            ]
            fs_tf_config.output_map.info_1.param_name = "left-context-posteriors"
            fs_tf_config.output_map.info_1.tensor_name = self.tensor_map[
                "out_left_context"
            ]
            fs_tf_config.output_map.info_2.param_name = "right-context-posteriors"
            fs_tf_config.output_map.info_2.tensor_name = self.tensor_map[
                "out_right_context"
            ]

        if self.context_type in [
            PhoneticContext.triphone_forward,
            PhoneticContext.tri_state_transition,
        ]:
            # outputs
            fs_tf_config.output_map.info_0.param_name = "right-context-posteriors"
            fs_tf_config.output_map.info_0.tensor_name = self.tensor_map[
                "out_right_context"
            ]
            fs_tf_config.output_map.info_1.param_name = "center-state-posteriors"
            fs_tf_config.output_map.info_1.tensor_name = self.tensor_map[
                "out_center_state"
            ]
            fs_tf_config.output_map.info_2.param_name = "left-context-posteriors"
            fs_tf_config.output_map.info_2.tensor_name = self.tensor_map[
                "out_left_context"
            ]

            if self.context_type == PhoneticContext.tri_state_transition:
                fs_tf_config.output_map.info_3.param_name = "delta-posteriors"
                fs_tf_config.output_map.info_3.tensor_name = self.tensor_map[
                    "out_delta"
                ]

        elif self.context_type == PhoneticContext.triphone_backward:
            # outputs
            fs_tf_config.output_map.info_0.param_name = "left-context-posteriors"
            fs_tf_config.output_map.info_0.tensor_name = self.tensor_map[
                "out_left_context"
            ]
            fs_tf_config.output_map.info_1.param_name = "right-context-posteriors"
            fs_tf_config.output_map.info_1.tensor_name = self.tensor_map[
                "out_right_context"
            ]
            fs_tf_config.output_map.info_2.param_name = "center-state-posteriors"
            fs_tf_config.output_map.info_2.tensor_name = self.tensor_map[
                "out_center_state"
            ]

        if self.is_multi_encoder_output:
            if self.context_type in [
                PhoneticContext.monophone,
                PhoneticContext.mono_state_transition,
            ]:
                fs_tf_config.input_map.info_1.param_name = "deltaEncoder-output"
                fs_tf_config.input_map.info_1.tensor_name = self.tensor_map[
                    "in_delta_encoder_output"
                ]
            else:
                fs_tf_config.input_map.info_2.param_name = "deltaEncoder-output"
                fs_tf_config.input_map.info_2.tensor_name = self.tensor_map[
                    "in_delta_encoder_output"
                ]

        self.featureScorerConfig = fs_tf_config

    def getFeatureFlow(self, feature_path, tf_flow):
        tf_feature_flow = rasr.FlowNetwork()
        base_mapping = tf_feature_flow.add_net(feature_path)
        tf_mapping = tf_feature_flow.add_net(tf_flow)
        tf_feature_flow.interconnect_inputs(feature_path, base_mapping)
        tf_feature_flow.interconnect(
            feature_path,
            base_mapping,
            tf_flow,
            tf_mapping,
            {"features": "input-features"},
        )
        tf_feature_flow.interconnect_outputs(tf_flow, tf_mapping)

        return tf_feature_flow

    def get_tfrnn_lm_config(
        self,
        name,
        scale,
        min_batch_size=1024,
        opt_batch_size=1024,
        max_batch_size=1024,
        allow_reduced_hist=None,
        async_lm=False,
        single_step_only=False,
    ):
        res = copy.deepcopy(self.tfrnn_lms[name])
        if allow_reduced_hist is not None:
            res.allow_reduced_history = allow_reduced_hist
        res.scale = scale
        res.min_batch_size = min_batch_size
        res.opt_batch_size = opt_batch_size
        res.max_batch_size = max_batch_size
        if async_lm:
            res["async"] = async_lm
        if async_lm or single_step_only:
            res.single_step_only = True

        return res

    def get_nce_lm(self, **kwargs):
        lstm_lm_config = self.get_tfrnn_lm_config(**kwargs)
        lstm_lm_config.output_map.info_1.param_name = "weights"
        lstm_lm_config.output_map.info_1.tensor_name = "output/W/read"
        lstm_lm_config.output_map.info_2.param_name = "bias"
        lstm_lm_config.output_map.info_2.tensor_name = "output/b/read"
        lstm_lm_config.softmax_adapter.type = "blas_nce"

        return lstm_lm_config

    def get_rnn_config(self, scale=1.0):
        lmConfigParams = {
            "name": "kazuki_real_nce",
            "min_batch_size": 1024,
            "opt_batch_size": 1024,
            "max_batch_size": 1024,
            "scale": scale,
            "allow_reduced_hist": True,
        }

        return self.get_nce_lm(**lmConfigParams)

    def get_rnn_full_config(self, scale=13.0):
        lmConfigParams = {
            "name": "kazuki_full",
            "min_batch_size": 4,
            "opt_batch_size": 64,
            "max_batch_size": 128,
            "scale": scale,
            "allow_reduced_hist": True,
        }
        return self.get_tfrnn_lm_config(**lmConfigParams)

    def add_tfrnn_lms(self):
        tfrnn_dir = "/u/beck/setups/swb1/2018-06-08_nnlm_decoding/dependencies/tfrnn"
        # backup:  '/work/asr4/raissi/ms-thesis-setups/dependencies/tfrnn'

        rnn_lm_config = rasr.RasrConfig()
        rnn_lm_config.type = "tfrnn"
        rnn_lm_config.vocab_file = tk.Path("%s/vocabulary" % tfrnn_dir)
        rnn_lm_config.transform_output_negate = True
        rnn_lm_config.vocab_unknown_word = "<unk>"

        rnn_lm_config.loader.type = "meta"
        rnn_lm_config.loader.meta_graph_file = tk.Path(
            "%s/network.019.meta" % tfrnn_dir
        )
        rnn_lm_config.loader.saved_model_file = rasr.StringWrapper(
            "%s/network.019" % tfrnn_dir, tk.Path("%s/network.019.index" % tfrnn_dir)
        )
        rnn_lm_config.loader.required_libraries = tk.Path(self.native_lstm_path)

        rnn_lm_config.input_map.info_0.param_name = "word"
        rnn_lm_config.input_map.info_0.tensor_name = (
            "extern_data/placeholders/delayed/delayed"
        )
        rnn_lm_config.input_map.info_0.seq_length_tensor_name = (
            "extern_data/placeholders/delayed/delayed_dim0_size"
        )

        rnn_lm_config.output_map.info_0.param_name = "softmax"
        rnn_lm_config.output_map.info_0.tensor_name = "output/output_batch_major"

        kazuki_fake_nce_lm = copy.deepcopy(rnn_lm_config)
        tfrnn_dir = "/work/asr3/beck/setups/swb1/2018-06-08_nnlm_decoding/dependencies/tfrnn_nce"
        kazuki_fake_nce_lm.vocab_file = tk.Path(
            "%s/vocabmap.freq_sorted.txt" % tfrnn_dir
        )
        kazuki_fake_nce_lm.loader.meta_graph_file = tk.Path(
            "%s/inference.meta" % tfrnn_dir
        )
        kazuki_fake_nce_lm.loader.saved_model_file = rasr.StringWrapper(
            "%s/network.018" % tfrnn_dir, tk.Path("%s/network.018.index" % tfrnn_dir)
        )

        kazuki_real_nce_lm = copy.deepcopy(kazuki_fake_nce_lm)
        kazuki_real_nce_lm.output_map.info_0.tensor_name = "sbn/output_batch_major"

        self.tfrnn_lms["kazuki_real_nce"] = kazuki_real_nce_lm

    def add_lstm_full(self):
        tfrnn_dir = (
            "/work/asr4/raissi/ms-thesis-setups/lm-sa-swb/dependencies/lstm-lm-kazuki"
        )

        rnn_lm_config = rasr.RasrConfig()
        rnn_lm_config.type = "tfrnn"
        rnn_lm_config.vocab_file = tk.Path(("/").join([tfrnn_dir, "vocabulary"]))
        rnn_lm_config.transform_output_negate = True
        rnn_lm_config.vocab_unknown_word = "<unk>"

        rnn_lm_config.loader.type = "meta"
        rnn_lm_config.loader.meta_graph_file = tk.Path(
            "%s/network.019.meta" % tfrnn_dir
        )
        rnn_lm_config.loader.saved_model_file = rasr.StringWrapper(
            "%s/network.019" % tfrnn_dir, tk.Path("%s/network.019.index" % tfrnn_dir)
        )
        rnn_lm_config.loader.required_libraries = tk.Path(self.native_lstm_path)

        rnn_lm_config.input_map.info_0.param_name = "word"
        rnn_lm_config.input_map.info_0.tensor_name = (
            "extern_data/placeholders/delayed/delayed"
        )
        rnn_lm_config.input_map.info_0.seq_length_tensor_name = (
            "extern_data/placeholders/delayed/delayed_dim0_size"
        )

        rnn_lm_config.output_map.info_0.param_name = "softmax"
        rnn_lm_config.output_map.info_0.tensor_name = "output/output_batch_major"

        self.tfrnn_lms["kazuki_full"] = rnn_lm_config

    def recognize_count_lm(
        self,
        *,
        label_info: LabelInfo,
        num_encoder_output: int,
        search_parameters: SearchParameters,
        calculate_stats=False,
        is_min_duration=False,
        opt_lm_am=True,
        only_lm_opt=True,
        keep_value=12,
        use_estimated_tdps=False,
        add_sis_alias_and_output=True,
        rerun_after_opt_lm=False,
        name_override: typing.Union[str, None] = None,
        name_prefix: str = "",
    ) -> RecognitionJobs:
        assert len(search_parameters.tdp_speech) == 4
        assert len(search_parameters.tdp_silence) == 4
        assert (
            not search_parameters.silence_penalties
            or len(search_parameters.silence_penalties) == 2
        )
        assert (
            not search_parameters.transition_scales
            or len(search_parameters.transition_scales) == 2
        )

        name = (
            name_override
            if name_override is not None
            else f"{name_prefix}{self.name}/"
            + "-".join(
                [
                    f"Beam{search_parameters.beam}",
                    f"Lm{search_parameters.lm_scale}",
                ]
            )
        )

        search_crp = copy.deepcopy(self.search_crp)

        if search_crp.lexicon_config.normalize_pronunciation:
            model_combination_config = rasr.RasrConfig()
            model_combination_config.pronunciation_scale = search_parameters.pron_scale
            pron_scale = search_parameters.pron_scale
        else:
            model_combination_config = None
            pron_scale = 1.0

        # additional search parameters
        if name_override is None:
            name += f"-Pron{pron_scale}"

        if name_override is None:
            if search_parameters.prior_info.left_context_prior is not None:
                name += f"-prL{search_parameters.prior_info.left_context_prior.scale}"
            if search_parameters.prior_info.center_state_prior is not None:
                name += f"-prC{search_parameters.prior_info.center_state_prior.scale}"
            if search_parameters.prior_info.right_context_prior is not None:
                name += f"-prR{search_parameters.prior_info.right_context_prior.scale}"

        loop_scale = forward_scale = 1.0
        sil_fwd_penalty = sil_loop_penalty = 0.0

        if search_parameters.tdp_scale is not None:
            if name_override is None:
                name += f"-tdpScale-{search_parameters.tdp_scale}"
                name += f"-tdpExit-{search_parameters.tdp_speech[-1]}"
                name += f"-silExit-{search_parameters.tdp_silence[-1]}"
                name += f"-tdpNWex-{20.0}"

            if search_parameters.transition_scales is not None:
                loop_scale, forward_scale = search_parameters.transition_scales

                if name_override is None:
                    name += f"-loopScale-{loop_scale}"
                    name += f"-fwdScale-{forward_scale}"

            if search_parameters.silence_penalties is not None:
                sil_loop_penalty, sil_fwd_penalty = search_parameters.silence_penalties

                if name_override is None:
                    name += f"-silLoopP-{sil_loop_penalty}"
                    name += f"-silFwdP-{sil_fwd_penalty}"

            if (
                search_parameters.tdp_speech[2] == "infinity"
                and search_parameters.tdp_silence[2] == "infinity"
                and name_override is None
            ):
                name += "-noSkip"
        else:
            if name_override is None:
                name += "-noTdp"

        if name_override is None:
            if search_parameters.we_pruning > 0.5:
                name += f"-wep{search_parameters.we_pruning}"
            if search_parameters.altas is not None:
                name += f"-ALTAS{search_parameters.altas}"
            if search_parameters.add_all_allophones:
                name += "-allAllos"

        state_tying = search_crp.acoustic_model_config.state_tying.type

        tdp_transition = (
            search_parameters.tdp_speech
            if search_parameters.tdp_scale is not None
            else (0.0, 0.0, "infinity", 0.0)
        )
        tdp_silence = (
            search_parameters.tdp_silence
            if search_parameters.tdp_scale is not None
            else (0.0, 0.0, "infinity", 0.0)
        )

        search_crp.acoustic_model_config = am.acoustic_model_config(
            state_tying=state_tying,
            states_per_phone=label_info.n_states_per_phone,
            state_repetitions=1,
            across_word_model=True,
            early_recombination=False,
            tdp_scale=search_parameters.tdp_scale,
            tdp_transition=tdp_transition,
            tdp_silence=tdp_silence,
        )

        search_crp.acoustic_model_config.allophones[
            "add-all"
        ] = search_parameters.add_all_allophones
        search_crp.acoustic_model_config.allophones[
            "add-from-lexicon"
        ] = not search_parameters.add_all_allophones

        search_crp.acoustic_model_config["state-tying"][
            "use-boundary-classes"
        ] = label_info.use_boundary_classes
        search_crp.acoustic_model_config["state-tying"][
            "use-word-end-classes"
        ] = label_info.use_word_end_classes

        # lm config update
        search_crp.language_model_config.scale = search_parameters.lm_scale

        rqms = self.get_requirements(beam=search_parameters.beam)
        sp = self.get_search_params(
            search_parameters.beam,
            search_parameters.beam_limit,
            search_parameters.we_pruning,
            search_parameters.we_pruning_limit,
            is_count_based=True,
        )

        if search_parameters.altas is not None:
            adv_search_extra_config = rasr.RasrConfig()
            adv_search_extra_config.flf_lattice_tool.network.recognizer.recognizer.acoustic_lookahead_temporal_approximation_scale = (
                search_parameters.altas
            )
        else:
            adv_search_extra_config = None

        feature_scorer = get_feature_scorer(
            context_type=self.context_type,
            label_info=label_info,
            feature_scorer_config=self.featureScorerConfig,
            mixtures=self.mixtures,
            silence_id=self.silence_id,
            prior_info=search_parameters.prior_info,
            posterior_scales=search_parameters.posterior_scales,
            num_label_contexts=label_info.n_contexts,
            num_states_per_phone=label_info.n_states_per_phone,
            num_encoder_output=num_encoder_output,
            loop_scale=loop_scale,
            forward_scale=forward_scale,
            silence_loop_penalty=sil_loop_penalty,
            silence_forward_penalty=sil_fwd_penalty,
            use_estimated_tdps=use_estimated_tdps,
            state_dependent_tdp_file=search_parameters.state_dependent_tdps,
            is_min_duration=is_min_duration,
            is_multi_encoder_output=self.is_multi_encoder_output,
        )

        pre_path = (
            "decoding-gridsearch" if search_parameters.altas is not None else "decoding"
        )

        search = recog.AdvancedTreeSearchJob(
            crp=search_crp,
            feature_flow=self.featureScorerFlow,
            feature_scorer=feature_scorer,
            search_parameters=sp,
            lm_lookahead=True,
            eval_best_in_lattice=True,
            use_gpu=self.gpu,
            rtf=rqms["rtf"],
            mem=rqms["mem"],
            cpu=4,
            model_combination_config=model_combination_config,
            model_combination_post_config=None,
            extra_config=adv_search_extra_config,
            extra_post_config=None,
        )

        if add_sis_alias_and_output:
            search.add_alias(f"{pre_path}/{name}")

        if calculate_stats:
            stat = ExtractSearchStatisticsJob(list(search.out_log_file.values()), 3.61)

            if add_sis_alias_and_output:
                stat.add_alias(f"statistics/{name}")
                tk.register_output(f"statistics/rtf/{name}.rtf", stat.decoding_rtf)
        else:
            stat = None

        if keep_value is not None:
            search.keep_value(keep_value)

        lat2ctm_extra_config = rasr.RasrConfig()
        lat2ctm_extra_config.flf_lattice_tool.network.to_lemma.links = "best"
        lat2ctm = recog.LatticeToCtmJob(
            crp=search_crp,
            lattice_cache=search.out_lattice_bundle,
            parallelize=True,
            best_path_algo="bellman-ford",
            extra_config=lat2ctm_extra_config,
            fill_empty_segments=True,
        )

        s_kwrgs = copy.copy(self.eval_files)
        s_kwrgs["hyp"] = lat2ctm.out_ctm_file
        scorer = recog.ScliteJob(**s_kwrgs)

        if add_sis_alias_and_output:
            tk.register_output(f"{pre_path}/{name}.wer", scorer.out_report_dir)

        if opt_lm_am and search_parameters.altas is None:
            assert search_parameters.beam >= 15.0

            opt = recog.OptimizeAMandLMScaleJob(
                crp=search_crp,
                lattice_cache=search.out_lattice_bundle,
                initial_am_scale=pron_scale,
                initial_lm_scale=search_parameters.lm_scale,
                scorer_cls=recog.ScliteJob,
                scorer_kwargs=s_kwrgs,
                opt_only_lm_scale=only_lm_opt,
            )

            if add_sis_alias_and_output:
                tk.register_output(
                    f"{pre_path}/{name}/onlyLmOpt{only_lm_opt}.optlm.txt",
                    opt.out_log_file,
                )

            if rerun_after_opt_lm:
                rounded = Delayed(opt.out_best_lm_score).function(round2)
                params = search_parameters.with_lm_scale(rounded)

                return self.recognize_count_lm(
                    opt_lm_am=False,
                    search_parameters=params,
                    name_override=f"{name}-optlm",
                    label_info=label_info,
                    num_encoder_output=num_encoder_output,
                    calculate_stats=calculate_stats,
                    is_min_duration=is_min_duration,
                    only_lm_opt=only_lm_opt,
                    keep_value=keep_value,
                    use_estimated_tdps=use_estimated_tdps,
                    rerun_after_opt_lm=rerun_after_opt_lm,
                    add_sis_alias_and_output=add_sis_alias_and_output,
                )

        return RecognitionJobs(
            lat2ctm=lat2ctm, sclite=scorer, search=search, search_stats=stat
        )

    def align(
        self,
        name,
        crp,
        rtf=10,
        mem=8,
        am_trainer_exe_path=None,
        default_tdp=True,
    ):
        align_crp = copy.deepcopy(crp)
        if am_trainer_exe_path is not None:
            align_crp.acoustic_model_trainer_exe = am_trainer_exe_path

        if default_tdp:
            v = (3.0, 0.0, "infinity", 0.0)
            sv = (0.0, 3.0, "infinity", 0.0)
            keys = ["loop", "forward", "skip", "exit"]
            for i, k in enumerate(keys):
                align_crp.acoustic_model_config.tdp["*"][k] = v[i]
                align_crp.acoustic_model_config.tdp["silence"][k] = sv[i]

        # make sure it is correct for the fh feature scorer scorer
        align_crp.acoustic_model_config.state_tying.type = "no-tying-dense"

        # make sure the FSA is not buggy
        align_crp.acoustic_model_config["*"][
            "fix-allophone-context-at-word-boundaries"
        ] = True
        align_crp.acoustic_model_config["*"][
            "transducer-builder-filter-out-invalid-allophones"
        ] = True
        align_crp.acoustic_model_config["*"]["allow-for-silence-repetitions"] = False
        align_crp.acoustic_model_config["*"]["fix-tdp-leaving-epsilon-arc"] = True

        alignment = mm.AlignmentJob(
            crp=align_crp,
            feature_flow=self.featureScorerFlow,
            feature_scorer=self.feature_scorer,
            use_gpu=self.gpu,
            rtf=rtf,
        )
        alignment.rqmt["cpu"] = 2
        alignment.rqmt["mem"] = 8
        alignment.add_alias(f"alignments/align_{name}")
        tk.register_output(
            "alignments/realignment-{}".format(name), alignment.out_alignment_bundle
        )
        return alignment
