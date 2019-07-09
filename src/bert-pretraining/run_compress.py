import numpy as np
import torch
import scipy
import argparse
import sys, os
import logging

from smallfry import compress

import utils
NUM_TOL = 1e-6
FEAT_SUBSAMPLE_RATE = 0.1

def read_npy_feature(file):
    feats = np.load(file)
    feats = [feats[name] for name in feats.files]
    return feats

def read_npy_label(file):
    label = np.load(file)
    return label

def get_feature_file(dataset, folder, datapart="train"):
    if datapart == "train":
        return folder + "/{}.train.feature.npz".format(dataset)
    elif datapart == "heldout":
        return folder + "/{}.heldout.feature.npz".format(dataset)
    elif datapart == "test":
        return folder + "/{}.test.feature.npz".format(dataset)
    else:
        raise Exception("Datapart", datapart, " not supported!")

def get_label_file(dataset, folder, datapart="train"):
    if datapart == "train":
        return folder + "/{}.train.label.npy".format(dataset)
    elif datapart == "heldout":
        return folder + "/{}.heldout.label.npy".format(dataset)
    elif datapart == "test":
        return folder + "/{}.test.label.npy".format(dataset)
    else:
        raise Exception("Datapart", datapart, " not supported!")

def load_procrustes_data(args):
    assert "train.feature.npz" in args.input_file
    dataset = args.dataset
    folder = os.path.dirname(args.input_file)
    feats_train = read_npy_feature(get_feature_file(dataset, folder, "train"))
    feats_heldout = read_npy_feature(get_feature_file(dataset, folder, "heldout"))
    feats_test = read_npy_feature(get_feature_file(dataset, folder, "test"))
    labels_train = read_npy_label(get_label_file(dataset, folder, "train"))
    labels_heldout = read_npy_label(get_label_file(dataset, folder, "heldout"))
    labels_test = read_npy_label(get_label_file(dataset, folder, "test"))
    return feats_train, feats_heldout, feats_test, \
        labels_train, labels_heldout, labels_test

def save_procrustes_data(args, feats_train, feats_heldout, feats_test,
    labels_train, labels_heldout, labels_test):
    folder = args.out_folder
    dataset = args.dataset
    np.savez(get_feature_file(dataset, folder, "train"), *feats_train)
    np.savez(get_feature_file(dataset, folder, "heldout"), *feats_heldout)
    np.savez(get_feature_file(dataset, folder, "test"), *feats_test)
    np.save(get_label_file(dataset, folder, "train"), labels_train)
    np.save(get_label_file(dataset, folder, "heldout"), labels_heldout)
    np.save(get_label_file(dataset, folder, "test"), labels_test)

def save_final_results_compress(args, range_limit):
    results = args.__dict__
    results["results"] = {"range_limit": range_limit}
    utils.save_to_json(results, args.out_folder + "/final_results.json")

def save_final_results_procrutes(args):
    results = args.__dict__
    utils.save_to_json(results, args.out_folder + "/final_results.json")

def compression(feats, labels, args):
    # we will directly save if we need 32 bit embeddings
    range_limit = None
    if args.nbit != 32:
        # subsample and concatenate
        subset_id = np.random.choice(np.arange(len(feats)), size=int(len(feats) * FEAT_SUBSAMPLE_RATE))
        feats_subset = [feats[i].copy() for i in subset_id]
        X = np.concatenate(feats_subset, axis=0)

        # assert the last axis is feature dimension
        assert feats_subset[0].shape[-1] == feats_subset[-1].shape[-1]
        assert feats_subset[1].shape[-1] == feats_subset[-1].shape[-1]
        
        # run the compression to each of the things in the list
        logging.info("Estimate range limit using sample shape " + str(X.shape))
        range_limit = compress.find_optimal_range(X, args.nbit, stochastic_round=False, tol=args.golden_sec_tol)
        logging.info("Range limit {}, max/min {}/{}, std {} ".format(
            range_limit, np.max(X), np.min(X), np.std(X)))
        compressed_feats = []
        for i, feat in enumerate(feats):
            comp_feat = compress._compress_uniform(feat, args.nbit, range_limit,
                stochastic_round=False, skip_quantize=False)
            np.copyto(dst=feat, src=comp_feat)
            assert np.max(feats[i]) - range_limit < NUM_TOL, "not clipped right max/limit {}/{}".format(np.max(feats[i]), range_limit)
            assert -range_limit - np.min(feats[i]) < NUM_TOL, "not clipped right max/limit {}/{}".format(np.min(feats[i]), -range_limit)
            assert np.unique(feats[i]).size <= 2**args.nbit, "more unique values than expected"
    return feats, labels

def procrustes(feats_train, feats_heldout, feats_test, train_labels, args):
    # sanity check the reference and main feature feature are indeed a pair
    # which are only different in terms of corpus they are trained on
    # we process both the train heldout and test feature in this function
    assert "wiki18" in args.input_file
    check_name = args.input_file.replace("wiki18", "wiki17")
    assert check_name == args.procrustes_ref_input_file
    assert ".train.feature.npz" in args.input_file

    # load reference feature
    logging.info("procrustes ref feature " + args.procrustes_ref_input_file)
    feats_ref = read_npy_feature(args.procrustes_ref_input_file)
    labels_ref = read_npy_label(args.procrustes_ref_input_file.replace(".feature.npz", ".label.npy"))
    np.testing.assert_array_equal(train_labels, labels_ref)

    # subsample and concatenate
    subset_id = np.random.choice(np.arange(len(feats_train)), size=int(len(feats_train) * FEAT_SUBSAMPLE_RATE))
    feats_subset = [feats_train[i].copy() for i in subset_id]
    feats_subset_ref = [feats_ref[i].copy() for i in subset_id]

    X = np.concatenate(feats_subset, axis=0)
    X_ref = np.concatenate(feats_subset_ref, axis=0)

    # assert the last axis is feature dimension
    assert feats_subset[0].shape[-1] == feats_subset[-1].shape[-1]
    assert feats_subset[1].shape[-1] == feats_subset[-1].shape[-1]
    # assert the subsampled features has matched shape
    assert X.shape == X_ref.shape
    logging.info("Performing procrustes using sample matrix of size " + str(X.shape))
    R, _ = scipy.linalg.orthogonal_procrustes(A=X, B=X_ref)
    feats_train_rot = []
    feats_heldout_rot = []
    feats_test_rot = []
    for feat in feats_train:
        feats_train_rot.append(feat @ R)
    for feat in feats_heldout:
        feats_heldout_rot.append(feat @ R)
    for feat in feats_test:
        feats_test_rot.append(feat @ R)
    return feats_train_rot, feats_heldout_rot, feats_test_rot


def main():
    # add arguments
    argparser = argparse.ArgumentParser(sys.argv[0], conflict_handler='resolve')
    argparser.add_argument("--job_type", type=str, default="compression", choices=["compression", "procrustes"])
    argparser.add_argument("--input_file", type=str, help="The feature file to be compressed.")
    argparser.add_argument("--procrustes_ref_input_file", type=str, help="For procrustes, this specifies the reference we rotate the input file feature to.")
    argparser.add_argument("--out_folder", type=str, help="The folder to contain the output")
    argparser.add_argument("--nbit", type=int, help="Number of bits for compressed features.")
    argparser.add_argument("--dataset", type=str, help="The dataset for asserting on the filenames. Only for on the fly check")
    argparser.add_argument("--seed", type=int, help="Random seeds for the sampleing process.")
    argparser.add_argument("--golden_sec_tol", type=float, default=1e-3,
        help="termination criterion for golden section search")
    args = argparser.parse_args()

    # assert args.dataset in args.input_file
    # assert "seed_{}".format(args.seed) in args.input_file

    utils.ensure_dir(args.out_folder)
    utils.init_logging(args.out_folder)

    # set random seeds
    utils.set_random_seed(args.seed)

    if args.job_type == "compression":
        # load the dataset
        feats = read_npy_feature(args.input_file)
        labels = read_npy_label(args.input_file.replace(".feature.npz", ".label.npy"))
        feats, labels = compression(feats, labels, args)
        save_final_results_compress(args, range_limit)
        # save the results back to the format
        out_file_name = os.path.basename(args.input_file)
        out_file_name = args.out_folder + "/" + out_file_name
        np.savez(out_file_name, *feats)
        np.save(out_file_name.replace(".feature.npz", ".label.npy"), labels)
    elif args.job_type == "procrustes":
        feats_train, feats_heldout, feats_test, \
            labels_train, labels_heldout, labels_test = load_procrustes_data(args)
        feats_train, feats_heldout, feats_test = procrustes(feats_train, feats_heldout, feats_test, labels_train, args)
        save_procrustes_data(args, feats_train, feats_heldout, feats_test,
            labels_train, labels_heldout, labels_test)
        save_final_results_procrutes(args)

    # TODO: the range limit is correct, compression is indeed carried out and it is properly copyed inplace
    # TODO: check a direct through saving case, check a compressed case to see the similarity
    # sanity check on if the tol is reasoanble

if __name__ == "__main__":
    main()
    
