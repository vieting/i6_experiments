from recipe.i6_experiments.users.schupp.hybrid_hmm_nn.pipeline.librispeech_hybrid_tim_refactor import LibrispeechHybridSystemTim


def get_returnn_rasr_args(
    system : LibrispeechHybridSystemTim,
    train_corpus_key = 'train-other-960',
    feature_name = 'gammatone',
    alignment_name = 'align_hmm',
    num_classes = 12001,
    num_epochs = 200,
    partition_epochs = {'train': 20, 'dev': 1},
):
    assert system.rasr_am_config_is_created, "please use system.create_rasr_am_config(...) first"

    train_feature_flow = system.feature_flows[train_corpus_key][feature_name]
    train_alignment = system.alignments[train_corpus_key][alignment_name]
    
    return {
        'train_crp': system.crp[train_corpus_key + '_train'],
        'dev_crp': system.crp[train_corpus_key + '_dev'],
        'feature_flow' : train_feature_flow,
        'alignment' : train_alignment,
        'num_classes': num_classes,
        'num_epochs' : num_epochs,
        'partition_epochs': partition_epochs
    }