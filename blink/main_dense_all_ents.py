# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import argparse
import json
import sys
from blink.index.faiss_indexer import DenseFlatIndexer, DenseHNSWFlatIndexer, DenseIVFFlatIndexer

from tqdm import tqdm
import logging
import torch
import numpy as np
from colorama import init
from termcolor import colored
import torch.nn.functional as F

import blink.ner as NER
from torch.utils.data import DataLoader, SequentialSampler, TensorDataset
from blink.biencoder.biencoder import BiEncoderRanker, load_biencoder, to_bert_input
from blink.biencoder.data_process import (
    process_mention_data,
    get_context_representation_single_mention,
    get_candidate_representation,
)
import blink.candidate_ranking.utils as utils
import math

from blink.vcg_utils.measures import entity_linking_tp_with_overlap

import os
import sys
from tqdm import tqdm
import pdb
import time


HIGHLIGHTS = [
    "on_red",
    "on_green",
    "on_yellow",
    "on_blue",
    "on_magenta",
    "on_cyan",
]

from transformers import BertTokenizer
tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")

def _print_colorful_text(input_tokens, tokenizer, pred_triples):
    """
    pred_triples:
        Assumes no overlapping triples
    """
    sort_idxs = sorted(range(len(pred_triples)), key=lambda idx: pred_triples[idx][1])

    init()  # colorful output
    msg = ""
    if pred_triples and (len(pred_triples) > 0):
        msg += tokenizer.decode(input_tokens[0 : int(pred_triples[sort_idxs[0]][1])])
        for i, idx in enumerate(sort_idxs):
            triple = pred_triples[idx]
            msg += " " + colored(
                tokenizer.decode(input_tokens[int(triple[1]) : int(triple[2])]),
                "grey",
                HIGHLIGHTS[idx % len(HIGHLIGHTS)],
            )
            if i < len(sort_idxs) - 1:
                msg += " " + tokenizer.decode(input_tokens[
                    int(triple[2]) : int(pred_triples[sort_idxs[i + 1]][1])
                ])
            else:
                msg += " " + tokenizer.decode(input_tokens[int(triple[2]) : ])
    else:
        msg = tokenizer.decode(input_tokens)
    print("\n" + str(msg) + "\n")


def _print_colorful_prediction(all_entity_preds, pred_triples, id2text, id2kb):
    sort_idxs = sorted(range(len(pred_triples)), key=lambda idx: pred_triples[idx][1])
    for idx in sort_idxs:
        print(colored(all_entity_preds[0]['pred_tuples_string'][idx][1], "grey", HIGHLIGHTS[idx % len(HIGHLIGHTS)]))
        if pred_triples[idx][0] in id2kb:
            print("    Wikidata ID: {}".format(id2kb[pred_triples[idx][0]]))
        print("    Title: {}".format(all_entity_preds[0]['pred_tuples_string'][idx][0]))
        print("    Score: {}".format(str(all_entity_preds[0]['scores'][idx])))
        print("    Triple: {}".format(str(pred_triples[idx])))
        print("    Text: {}".format(id2text[pred_triples[idx][0]]))


def _load_candidates(
    entity_catalogue, entity_encoding, entity_token_ids,
    faiss_index="none", index_path=None,
    logger=None,
):
    logger.info("Loading candidate encodings")
    if faiss_index == "none":
        candidate_encoding = torch.load(entity_encoding)
        indexer = None
    else:
        if logger:
            logger.info("Using faiss index to retrieve entities.")
        candidate_encoding = None
        assert index_path is not None, "Error! Empty indexer path."
        if faiss_index == "flat":
            indexer = DenseFlatIndexer(1)
        elif faiss_index == "hnsw":
            indexer = DenseHNSWFlatIndexer(1)
        elif faiss_index == "ivfflat":
            indexer = DenseIVFFlatIndexer(1)
        else:
            raise ValueError("Error! Unsupported indexer type! Choose from flat,hnsw,ivfflat.")
        indexer.deserialize_from(index_path)

    candidate_encoding = torch.load(entity_encoding)
    candidate_token_ids = torch.load(entity_token_ids)
    logger.info("Finished loading candidate encodings")

    logger.info("Loading id2title")
    id2title = json.load(open("models/id2title.json"))
    logger.info("Finish loading id2title")
    logger.info("Loading id2text")
    id2text = json.load(open("models/id2text.json"))
    logger.info("Finish loading id2text")
    logger.info("Loading id2kb")
    id2kb = json.load(open("models/id2kb.json"))
    logger.info("Finish loading id2kb")

    return (
        candidate_encoding, candidate_token_ids, indexer, 
        id2title, id2text, id2kb,
    )


def _get_test_samples(
    test_filename, test_entities_path, logger,
):
    """
    Parses jsonl format with one example per line
    Each line of the following form

    IF HAVE LABELS
    {
        "id": "WebQTest-12",
        "text": "who is governor of ohio 2011?",
        "mentions": [[19, 23], [7, 15]],
        "tokenized_text_ids": [2040, 2003, 3099, 1997, 4058, 2249, 1029],
        "tokenized_mention_idxs": [[4, 5], [2, 3]],
        "label_id": [10902, 28422],
        "wikidata_id": ["Q1397", "Q132050"],
        "entity": ["Ohio", "Governor"],
        "label": [list of wikipedia descriptions]
    }

    IF NO LABELS (JUST PREDICTION)
    {
        "id": "WebQTest-12",
        "text": "who is governor of ohio 2011?",
    }
    """
    logger.info("Loading test samples....")
    test_samples = []
    unknown_entity_samples = []
    num_unknown_entity_samples = 0
    num_no_gold_entity = 0
    ner_errors = 0

    with open(test_filename, "r") as fin:
        lines = fin.readlines()
        sample_idx = 0
        do_setup_samples = True
        for i, line in enumerate(tqdm(lines)):
            record = json.loads(line)
            test_samples.append(record)

    logger.info("Finished loading test samples")

    return test_samples, num_unknown_entity_samples


def _process_biencoder_dataloader(samples, tokenizer, biencoder_params, logger):
    """
    Samples: list of examples, each of the form--

    IF HAVE LABELS
    {
        "id": "WebQTest-12",
        "text": "who is governor of ohio 2011?",
        "mentions": [[19, 23], [7, 15]],
        "tokenized_text_ids": [2040, 2003, 3099, 1997, 4058, 2249, 1029],
        "tokenized_mention_idxs": [[4, 5], [2, 3]],
        "label_id": [10902, 28422],
        "wikidata_id": ["Q1397", "Q132050"],
        "entity": ["Ohio", "Governor"],
        "label": [list of wikipedia descriptions]
    }

    IF NO LABELS (JUST PREDICTION)
    {
        "id": "WebQTest-12",
        "text": "who is governor of ohio 2011?",
    }
    """
    if 'label_id' in samples[0]:
        # have labels
        tokens_data, tensor_data_tuple, _ = process_mention_data(
            samples=samples,
            tokenizer=tokenizer,
            max_context_length=biencoder_params["max_context_length"],
            max_cand_length=biencoder_params["max_cand_length"],
            silent=False,
            logger=logger,
            debug=biencoder_params["debug"],
            add_mention_bounds=(not biencoder_params.get("no_mention_bounds", False)),
        )
    else:
        samples_text_tuple = []
        max_seq_len = 0
        for sample in samples:
            samples_text_tuple
            encoded_sample = [101] + tokenizer.encode(sample['text']) + [102]
            max_seq_len = max(len(encoded_sample), max_seq_len)
            samples_text_tuple.append(encoded_sample + [0 for _ in range(biencoder_params["max_context_length"] - len(encoded_sample))])
        tensor_data_tuple = [torch.tensor(samples_text_tuple)]
    tensor_data = TensorDataset(*tensor_data_tuple)
    sampler = SequentialSampler(tensor_data)
    dataloader = DataLoader(
        tensor_data, sampler=sampler, batch_size=biencoder_params["eval_batch_size"]
    )
    return dataloader


def _batch_reshape_mask_left(
    input_t, selected, pad_idx=0, left_align_mask=None
):
    """
    Left-aligns all ``selected" values in input_t, which is a batch of examples.
        - input_t: >=2D tensor (N, M, *)
        - selected: 2D torch.Bool tensor, 2 dims same size as first 2 dims of `input_t` (N, M) 
        - pad_idx represents the padding to be used in the output
        - left_align_mask: if already precomputed, pass the alignment mask in
    Example:
        input_t  = [[1,2,3,4],[5,6,7,8]]
        selected = [[0,1,0,1],[1,1,0,1]]
        output   = [[2,4,0],[5,6,8]]
    """
    batch_num_selected = selected.sum(1)
    max_num_selected = batch_num_selected.max()

    # (bsz, 2)
    repeat_freqs = torch.stack([batch_num_selected, max_num_selected - batch_num_selected], dim=-1)
    # (bsz x 2,)
    repeat_freqs = repeat_freqs.view(-1)

    if left_align_mask is None:
        # (bsz, 2)
        left_align_mask = torch.zeros(input_t.size(0), 2).to(input_t.device).bool()
        left_align_mask[:,0] = 1
        # (bsz x 2,): [1,0,1,0,...]
        left_align_mask = left_align_mask.view(-1)
        # (bsz x max_num_selected,): [1 xrepeat_freqs[0],0 x(M-repeat_freqs[0]),1 xrepeat_freqs[1],0 x(M-repeat_freqs[1]),...]
        left_align_mask = left_align_mask.repeat_interleave(repeat_freqs)
        # (bsz, max_num_selected)
        left_align_mask = left_align_mask.view(-1, max_num_selected)

    # reshape to (bsz, max_num_selected, *)
    input_reshape = torch.Tensor(left_align_mask.size() + input_t.size()[2:]).to(input_t.device, input_t.dtype).fill_(pad_idx)
    input_reshape[left_align_mask] = input_t[selected]
    # (bsz, max_num_selected, *); (bsz, max_num_selected)
    return input_reshape, left_align_mask


def _run_biencoder(
    args, biencoder, dataloader, candidate_encoding, samples,
    num_cand_mentions=100, num_cand_entities=10,  # TODO don't hardcode
    device="cpu", sample_to_all_context_inputs=None,
    threshold=0.0, indexer=None,
):
    """
    Returns: tuple
        labels (List[int]) [(max_num_mentions_gold) x exs]: gold labels -- returns None if no labels
        nns (List[Array[int]]) [(# of pred mentions, cands_per_mention) x exs]: predicted entity IDs in each example
        dists (List[Array[float]]) [(# of pred mentions, cands_per_mention) x exs]: scores of each entity in nns
        pred_mention_bounds (List[Array[int]]) [(# of pred mentions, 2) x exs]: predicted mention boundaries in each examples
        mention_scores (List[Array[float]]) [(# of pred mentions,) x exs]: mention score logit
        cand_scores (List[Array[float]]) [(# of pred mentions, cands_per_mention) x exs]: candidate score logit
    """
    biencoder.model.eval()
    biencoder_model = biencoder.model
    if hasattr(biencoder.model, "module"):
        biencoder_model = biencoder.model.module

    context_inputs = []
    nns = []
    dists = []
    mention_dists = []
    pred_mention_bounds = []
    mention_scores = []
    cand_scores = []
    sample_idx = 0
    ctxt_idx = 0
    label_ids = None
    for step, batch in enumerate(tqdm(dataloader)):
        context_input = batch[0]
        with torch.no_grad():
            token_idx_ctxt, segment_idx_ctxt, mask_ctxt = to_bert_input(context_input, biencoder.NULL_IDX)
            if device != "cpu":
                token_idx_ctxt = token_idx_ctxt.to(device)
                segment_idx_ctxt = segment_idx_ctxt.to(device)
                mask_ctxt = mask_ctxt.to(device)
            
            '''
            PREPARE INPUTS
            '''
            # (bsz, seqlen, embed_dim)
            context_encoding, _, _ = biencoder_model.context_encoder.bert_model(
                token_idx_ctxt, segment_idx_ctxt, mask_ctxt,
            )

            '''
            GET MENTION SCORES
            '''
            # (num_total_mentions,); (num_total_mentions,)
            mention_logits, mention_bounds = biencoder_model.classification_heads['mention_scores'](context_encoding, mask_ctxt)

            '''
            PRUNE MENTIONS BASED ON SCORES (for each instance in batch, num_cand_mentions (>= -inf) OR THRESHOLD)
            '''
            '''
            topK_mention_scores, mention_pos = torch.cat([torch.arange(), mention_logits.topk(num_cand_mentions, dim=1)])
            mention_pos = mention_pos.flatten()
            '''
            # DIM (num_total_mentions, embed_dim)
            # (bsz, num_cand_mentions); (bsz, num_cand_mentions)
            top_mention_logits, mention_pos = mention_logits.topk(num_cand_mentions, sorted=True)
            # 2nd part of OR for if nothing is > 0
            # DIM (bsz, num_cand_mentions, 2)
            #   [:,:,0]: index of batch
            #   [:,:,1]: index into top mention in mention_bounds
            mention_pos = torch.stack([torch.arange(mention_pos.size(0)).to(mention_pos.device).unsqueeze(-1).expand_as(mention_pos), mention_pos], dim=-1)
            # DIM (bsz, num_cand_mentions)
            top_mention_pos_mask = torch.sigmoid(top_mention_logits).log() > threshold
            # DIM (total_possible_mentions, 2)
            #   tuples of [index of batch, index into mention_bounds] of what mentions to include
            mention_pos = mention_pos[top_mention_pos_mask | (
                # If nothing is > threshold, use topK that are > -inf (2nd part of OR)
                ((top_mention_pos_mask.sum(1) == 0).unsqueeze(-1)) & (top_mention_logits > -float("inf"))
            )]  #(mention_pos_2_mask.sum(1) == 0).unsqueeze(-1)]
            mention_pos = mention_pos.view(-1, 2)  #2297 [45,11]
            # DIM (bs, total_possible_mentions)
            #   mask of possible logits
            mention_pos_mask = torch.zeros(mention_logits.size(), dtype=torch.bool).to(mention_pos.device)
            mention_pos_mask[mention_pos[:,0], mention_pos[:,1]] = 1
            # DIM (bs, max_num_pred_mentions, 2)
            chosen_mention_bounds, left_align_mask = _batch_reshape_mask_left(mention_bounds, mention_pos_mask, pad_idx=0)
            # DIM (bs, max_num_pred_mentions)
            chosen_mention_logits, _ = _batch_reshape_mask_left(mention_logits, mention_pos_mask, pad_idx=-float("inf"), left_align_mask=left_align_mask)

            '''
            GET CANDIDATE SCORES + TOP CANDIDATES PER MENTION
            '''
            # (bs, max_num_pred_mentions, embed_dim)
            embedding_ctxt = biencoder_model.classification_heads['get_context_embeds'](context_encoding, chosen_mention_bounds)
            if biencoder_model.linear_compression is not None:
                embedding_ctxt = biencoder_model.linear_compression(embedding_ctxt)
            # (all_pred_mentions_batch, embed_dim)
            embedding_ctxt = embedding_ctxt[left_align_mask]

            if indexer is None:
                try:
                    # start_time = time.time()
                    if embedding_ctxt.size(0) > 1:
                        embedding_ctxt = embedding_ctxt.squeeze(0)
                    # DIM (all_pred_mentions_batch, all_cand_entities)
                    cand_logits = embedding_ctxt.mm(candidate_encoding.to(device).t())
                    # DIM (all_pred_mentions_batch, num_cand_entities); (all_pred_mentions_batch, num_cand_entities)
                    top_cand_logits_shape, top_cand_indices_shape = cand_logits.topk(num_cand_entities, dim=-1, sorted=True)
                    # end_time = time.time()
                except:
                    # for memory savings, go through one chunk of candidates at a time
                    SPLIT_SIZE=1000000
                    done=False
                    while not done:
                        top_cand_logits_list = []
                        top_cand_indices_list = []
                        max_chunk = int(len(candidate_encoding) / SPLIT_SIZE)
                        for chunk_idx in range(max_chunk):
                            try:
                                # DIM (num_total_mentions, num_cand_entities); (num_total_mention, num_cand_entities)
                                top_cand_logits, top_cand_indices = embedding_ctxt.mm(candidate_encoding[chunk_idx*SPLIT_SIZE:(chunk_idx+1)*SPLIT_SIZE].to(device).t().contiguous()).topk(10, dim=-1, sorted=True)
                                top_cand_logits_list.append(top_cand_logits)
                                top_cand_indices_list.append(top_cand_indices + chunk_idx*SPLIT_SIZE)
                                if len((top_cand_indices_list[chunk_idx] < 0).nonzero()) > 0:
                                    import pdb
                                    pdb.set_trace()
                            except:
                                SPLIT_SIZE = int(SPLIT_SIZE/2)
                                break
                        if len(top_cand_indices_list) == max_chunk:
                            # DIM (num_total_mentions, num_cand_entities); (num_total_mentions, num_cand_entities) -->
                            #       top_top_cand_indices_shape indexes into top_cand_indices
                            top_cand_logits_shape, top_top_cand_indices_shape = torch.cat(
                                top_cand_logits_list, dim=-1).topk(num_cand_entities, dim=-1, sorted=True)
                            # make indices index into candidate_encoding
                            # DIM (num_total_mentions, max_chunk*num_cand_entities)
                            all_top_cand_indices = torch.cat(top_cand_indices_list, dim=-1)
                            # DIM (num_total_mentions, num_cand_entities)
                            top_cand_indices_shape = all_top_cand_indices.gather(-1, top_top_cand_indices_shape)
                            done = True
            else:
                # DIM (all_pred_mentions_batch, num_cand_entities); (all_pred_mentions_batch, num_cand_entities)
                top_cand_logits_shape, top_cand_indices_shape = indexer.search_knn(embedding_ctxt.cpu().numpy(), num_cand_entities)
                top_cand_logits_shape = torch.tensor(top_cand_logits_shape).to(embedding_ctxt.device)
                top_cand_indices_shape = torch.tensor(top_cand_indices_shape).to(embedding_ctxt.device)

            # DIM (bs, max_num_pred_mentions, num_cand_entities)
            top_cand_logits = torch.zeros(chosen_mention_logits.size(0), chosen_mention_logits.size(1), top_cand_logits_shape.size(-1)).to(
                top_cand_logits_shape.device, top_cand_logits_shape.dtype)
            top_cand_logits[left_align_mask] = top_cand_logits_shape
            top_cand_indices = torch.zeros(chosen_mention_logits.size(0), chosen_mention_logits.size(1), top_cand_indices_shape.size(-1)).to(
                top_cand_indices_shape.device, top_cand_indices_shape.dtype)
            top_cand_indices[left_align_mask] = top_cand_indices_shape

            '''
            COMPUTE FINAL SCORES FOR EACH CAND-MENTION PAIR + PRUNE USING IT
            '''
            # Has NAN for impossible mentions...
            # log p(entity && mb) = log [p(entity|mention bounds) * p(mention bounds)] = log p(e|mb) + log p(mb)
            # DIM (bs, max_num_pred_mentions, num_cand_entities)
            scores = torch.log_softmax(top_cand_logits, -1) + torch.sigmoid(chosen_mention_logits.unsqueeze(-1)).log()

            '''
            DON'T NEED TO RESORT BY NEW SCORE -- DISTANCE PRESERVING (largest entity score still be largest entity score)
            '''
    
            for idx in range(len(batch[0])):
                # TODO do with masking....!!!
                # [(seqlen) x exs] <= (bsz, seqlen)
                context_inputs.append(context_input[idx][mask_ctxt[idx]].data.cpu().numpy())
                if len(top_cand_indices[idx][top_cand_indices[idx] < 0]) > 0:
                    import pdb
                    pdb.set_trace()
                # [(max_num_mentions, cands_per_mention) x exs] <= (bsz, max_num_mentions=num_cand_mentions, cands_per_mention)
                nns.append(top_cand_indices[idx][left_align_mask[idx]].data.cpu().numpy())
                # [(max_num_mentions, cands_per_mention) x exs] <= (bsz, max_num_mentions=num_cand_mentions, cands_per_mention)
                dists.append(scores[idx][left_align_mask[idx]].data.cpu().numpy())
                # [(max_num_mentions, 2) x exs] <= (bsz, max_num_mentions=num_cand_mentions, 2)
                pred_mention_bounds.append(chosen_mention_bounds[idx][left_align_mask[idx]].data.cpu().numpy())
                # [(max_num_mentions,) x exs] <= (bsz, max_num_mentions=num_cand_mentions)
                mention_scores.append(chosen_mention_logits[idx][left_align_mask[idx]].data.cpu().numpy())
                # [(max_num_mentions, cands_per_mention) x exs] <= (bsz, max_num_mentions=num_cand_mentions, cands_per_mention)
                cand_scores.append(top_cand_logits[idx][left_align_mask[idx]].data.cpu().numpy())

    return nns, dists, pred_mention_bounds, mention_scores, cand_scores


def get_predictions(
    args, dataloader, biencoder_params, samples, nns, dists, mention_scores, cand_scores,
    pred_mention_bounds, id2title, threshold=-2.9, mention_threshold=-0.6931,
):
    """
    Arguments:
        args, dataloader, biencoder_params, samples, nns, dists, pred_mention_bounds
    Returns:
        all_entity_preds,
        num_correct_weak, num_correct_strong, num_predicted, num_gold,
        num_correct_weak_from_input_window, num_correct_strong_from_input_window, num_gold_from_input_window
    """

    # save biencoder predictions and print precision/recalls
    num_correct_weak = 0
    num_correct_strong = 0
    num_predicted = 0
    num_gold = 0
    num_correct_weak_from_input_window = 0
    num_correct_strong_from_input_window = 0
    num_gold_from_input_window = 0
    all_entity_preds = []

    f = errors_f = None
    if getattr(args, 'save_preds_dir', None) is not None:
        save_biencoder_file = os.path.join(args.save_preds_dir, 'biencoder_outs.jsonl')
        f = open(save_biencoder_file, 'w')
        errors_f = open(os.path.join(args.save_preds_dir, 'biencoder_errors.jsonl'), 'w')

    # nns (List[Array[int]]) [(num_pred_mentions, cands_per_mention) x exs])
    # dists (List[Array[float]]) [(num_pred_mentions, cands_per_mention) x exs])
    # pred_mention_bounds (List[Array[int]]) [(num_pred_mentions, 2) x exs]
    # cand_scores (List[Array[float]]) [(num_pred_mentions, cands_per_mention) x exs])
    # mention_scores (List[Array[float]]) [(num_pred_mentions,) x exs])
    for batch_num, batch_data in enumerate(dataloader):
        batch_context = batch_data[0]
        if len(batch_data) > 1:
            _, batch_cands, batch_label_ids, batch_mention_idxs, batch_mention_idx_masks = batch_data
        for b in range(len(batch_context)):
            i = batch_num * biencoder_params['eval_batch_size'] + b
            sample = samples[i]
            input_context = batch_context[b][batch_context[b] != 0].tolist()  # filter out padding

            # (num_pred_mentions, cands_per_mention)
            scores = dists[i] if args.threshold_type == "joint" else cand_scores[i]
            cands_mask = (scores[:,0] == scores[:,0])
            pred_entity_list = nns[i][cands_mask]
            if len(pred_entity_list) > 0:
                e_id = pred_entity_list[0]
            distances = scores[cands_mask]
            # (num_pred_mentions, 2)
            entity_mention_bounds_idx = pred_mention_bounds[i][cands_mask]
            utterance = sample['text']

            if args.threshold_type == "joint":
                # THRESHOLDING
                assert utterance is not None
                top_mentions_mask = (distances[:,0] > threshold)
            elif args.threshold_type == "top_entity_by_mention":
                top_mentions_mask = (mention_scores[i] > mention_threshold)
            elif args.threshold_type == "thresholded_entity_by_mention":
                top_mentions_mask = (distances[:,0] > threshold) & (mention_scores[i] > mention_threshold)
    
            _, sort_idxs = torch.tensor(distances[:,0][top_mentions_mask]).sort(descending=True)
            # cands already sorted by score
            all_pred_entities = pred_entity_list[:,0][top_mentions_mask]
            e_mention_bounds = entity_mention_bounds_idx[top_mentions_mask]
            chosen_distances = distances[:,0][top_mentions_mask]
            if len(all_pred_entities) >= 2:
                all_pred_entities = all_pred_entities[sort_idxs]
                e_mention_bounds = e_mention_bounds[sort_idxs]
                chosen_distances = chosen_distances[sort_idxs]

            # prune mention overlaps
            e_mention_bounds_pruned = []
            all_pred_entities_pruned = []
            chosen_distances_pruned = []
            mention_masked_utterance = np.zeros(len(input_context))
            # ensure well-formed-ness, prune overlaps
            # greedily pick highest scoring, then prune all overlapping
            for idx, mb in enumerate(e_mention_bounds):
                mb[1] += 1  # prediction was inclusive, now make exclusive
                # check if in existing mentions
                try:
                    if mention_masked_utterance[mb[0]:mb[1]].sum() >= 1:
                        continue
                except:
                    import pdb
                    pdb.set_trace()
                e_mention_bounds_pruned.append(mb)
                all_pred_entities_pruned.append(all_pred_entities[idx])
                chosen_distances_pruned.append(float(chosen_distances[idx]))
                mention_masked_utterance[mb[0]:mb[1]] = 1

            input_context = input_context[1:-1]  # remove BOS and sep
            pred_triples = [(
                # sample['all_gold_entities'][i],
                str(all_pred_entities_pruned[j]),
                int(e_mention_bounds_pruned[j][0]) - 1,  # -1 for BOS
                int(e_mention_bounds_pruned[j][1]) - 1,
            ) for j in range(len(all_pred_entities_pruned))]

            entity_results = {
                "id": sample["id"],
                "text": sample["text"],
                "scores": chosen_distances_pruned,
            }

            if 'label_id' in sample:
                # Get LABELS
                input_mention_idxs = batch_mention_idxs[b][batch_mention_idx_masks[b]].tolist()
                input_label_ids = batch_label_ids[b][batch_label_ids[b] != -1].tolist()
                assert len(input_label_ids) == len(input_mention_idxs)
                gold_mention_bounds = [
                    sample['text'][ment[0]-10:ment[0]] + "[" + sample['text'][ment[0]:ment[1]] + "]" + sample['text'][ment[1]:ment[1]+10]
                    for ment in sample['mentions']
                ]

                # GET ALIGNED MENTION_IDXS (input is slightly different to model) between ours and gold labels -- also have to account for BOS
                gold_input = sample['tokenized_text_ids']
                # return first instance of my_input in gold_input
                for my_input_start in range(len(gold_input)):
                    if (
                        gold_input[my_input_start] == input_context[0] and
                        gold_input[my_input_start:my_input_start+len(input_context)] == input_context
                    ):
                        break

                # add alignment factor (my_input_start) to predicted mention triples
                pred_triples = [(
                    triple[0], triple[1] + my_input_start, triple[2] + my_input_start,
                ) for triple in pred_triples]
                gold_triples = [(
                    str(sample['label_id'][j]),
                    sample['tokenized_mention_idxs'][j][0], sample['tokenized_mention_idxs'][j][1],
                ) for j in range(len(sample['label_id']))]
                num_overlap_weak, num_overlap_strong = entity_linking_tp_with_overlap(gold_triples, pred_triples)
                num_correct_weak += num_overlap_weak
                num_correct_strong += num_overlap_strong
                num_predicted += len(all_pred_entities_pruned)
                num_gold += len(sample["label_id"])

                # compute number correct given the input window
                pred_input_window_triples = [(
                    # sample['all_gold_entities'][i],
                    str(all_pred_entities_pruned[j]),
                    int(e_mention_bounds_pruned[j][0]), int(e_mention_bounds_pruned[j][1]),
                ) for j in range(len(all_pred_entities_pruned))]
                gold_input_window_triples = [(
                    str(input_label_ids[j]),
                    input_mention_idxs[j][0], input_mention_idxs[j][1] + 1,
                ) for j in range(len(input_label_ids))]
                num_overlap_weak_window, num_overlap_strong_window = entity_linking_tp_with_overlap(gold_input_window_triples, pred_input_window_triples)
                num_correct_weak_from_input_window += num_overlap_weak_window
                num_correct_strong_from_input_window += num_overlap_strong_window
                num_gold_from_input_window += len(input_mention_idxs)

                for triple in pred_triples:
                    if triple[0] not in id2title:
                        import pdb
                        pdb.set_trace()
                entity_results.update({
                    "pred_tuples_string": [
                        [id2title[triple[0]], tokenizer.decode(sample['tokenized_text_ids'][triple[1]:triple[2]])]
                        for triple in pred_triples
                    ],
                    "gold_tuples_string": [
                        [id2title[triple[0]], tokenizer.decode(sample['tokenized_text_ids'][triple[1]:triple[2]])]
                        for triple in gold_triples
                    ],
                    "pred_triples": pred_triples,
                    "gold_triples": gold_triples,
                    "tokens": input_context,
                })

                if errors_f is not None and (num_overlap_weak != len(gold_triples) or num_overlap_weak != len(pred_triples)):
                    errors_f.write(json.dumps(entity_results) + "\n")
            else:
                entity_results.update({
                    "pred_tuples_string": [
                        [id2title[triple[0]], tokenizer.decode(input_context[triple[1]:triple[2]])]
                        for triple in pred_triples
                    ],
                    "pred_triples": pred_triples,
                    "tokens": input_context,
                })

            all_entity_preds.append(entity_results)
            if f is not None:
                f.write(
                    json.dumps(entity_results) + "\n"
                )
    
    if f is not None:
        f.close()
        errors_f.close()
    return (
        all_entity_preds, num_correct_weak, num_correct_strong, num_predicted, num_gold,
        num_correct_weak_from_input_window, num_correct_strong_from_input_window, num_gold_from_input_window
    )


def _save_biencoder_outs(save_preds_dir, nns, dists, pred_mention_bounds, cand_scores, mention_scores, runtime):
    np.save(os.path.join(args.save_preds_dir, "biencoder_nns.npy"), nns)
    np.save(os.path.join(args.save_preds_dir, "biencoder_dists.npy"), dists)
    np.save(os.path.join(args.save_preds_dir, "biencoder_mention_bounds.npy"), pred_mention_bounds)
    np.save(os.path.join(args.save_preds_dir, "biencoder_cand_scores.npy"), cand_scores)
    np.save(os.path.join(args.save_preds_dir, "biencoder_mention_scores.npy"), mention_scores)
    with open(os.path.join(args.save_preds_dir, "runtime.txt"), "w") as wf:
        wf.write(str(runtime))


def _load_biencoder_outs(save_preds_dir):
    nns = np.load(os.path.join(save_preds_dir, "biencoder_nns.npy"), allow_pickle=True)
    dists = np.load(os.path.join(save_preds_dir, "biencoder_dists.npy"), allow_pickle=True)
    pred_mention_bounds = np.load(os.path.join(save_preds_dir, "biencoder_mention_bounds.npy"), allow_pickle=True)
    cand_scores = np.load(os.path.join(save_preds_dir, "biencoder_cand_scores.npy"), allow_pickle=True)
    mention_scores = np.load(os.path.join(save_preds_dir, "biencoder_mention_scores.npy"), allow_pickle=True)
    runtime = float(open(os.path.join(args.save_preds_dir, "runtime.txt")).read())
    return nns, dists, pred_mention_bounds, cand_scores, mention_scores, runtime


def display_metrics(
    num_correct, num_predicted, num_gold, prefix="",
):
    p = 0 if num_predicted == 0 else float(num_correct) / float(num_predicted)
    r = 0 if num_gold == 0 else float(num_correct) / float(num_gold)
    if p + r > 0:
        f1 = 2 * p * r / (p + r)
    else:
        f1 = 0
    print("{0}precision = {1} / {2} = {3}".format(prefix, num_correct, num_predicted, p))
    print("{0}recall = {1} / {2} = {3}".format(prefix, num_correct, num_gold, r))
    print("{0}f1 = {1}".format(prefix, f1))


def load_models(args, logger):
    # load biencoder model
    logger.info("loading biencoder model")
    try:
        with open(args.biencoder_config) as json_file:
            biencoder_params = json.load(json_file)
    except json.decoder.JSONDecodeError:
        with open(args.biencoder_config) as json_file:
            for line in json_file:
                line = line.replace("'", "\"")
                line = line.replace("True", "true")
                line = line.replace("False", "false")
                line = line.replace("None", "null")
                biencoder_params = json.loads(line)
                break
    biencoder_params["path_to_model"] = args.biencoder_model
    biencoder_params["eval_batch_size"] = args.eval_batch_size
    biencoder_params["no_cuda"] = not args.use_cuda
    if biencoder_params["no_cuda"]:
        biencoder_params["data_parallel"] = False
    biencoder_params["load_cand_enc_only"] = False
    if getattr(args, 'max_context_length', None) is not None:
        biencoder_params["max_context_length"] = args.max_context_length
    # biencoder_params["mention_aggregation_type"] = args.mention_aggregation_type
    biencoder = load_biencoder(biencoder_params)
    if not args.use_cuda and type(biencoder.model).__name__ == 'DataParallel':
        biencoder.model = biencoder.model.module
    elif args.use_cuda and type(biencoder.model).__name__ != 'DataParallel':
        biencoder.model = torch.nn.DataParallel(biencoder.model)

    # load candidate entities
    logger.info("loading candidate entities")

    (
        candidate_encoding,
        candidate_token_ids,
        indexer,
        id2title,
        id2text,
        id2kb,
    ) = _load_candidates(
        args.entity_catalogue, args.entity_encoding, args.entity_token_ids,
        args.faiss_index, args.index_path, logger=logger,
    )

    return (
        biencoder,
        biencoder_params,
        candidate_encoding,
        candidate_token_ids,
        indexer,
        id2title,
        id2text,
        id2kb,
    )


def run(
    args,
    logger,
    biencoder,
    biencoder_params,
    candidate_encoding,
    candidate_token_ids,
    indexer,
    id2title,
    id2text,
    id2kb,
):

    if not args.test_mentions and not args.interactive:
        msg = (
            "ERROR: either you start BLINK with the "
            "interactive option (-i) or you pass in input test mentions (--test_mentions)"
            "and test entities (--test_entities)"
        )
        raise ValueError(msg)
    
    if getattr(args, 'save_preds_dir', None) is not None and not os.path.exists(args.save_preds_dir):
        os.makedirs(args.save_preds_dir)
        print("Saving preds in {}".format(args.save_preds_dir))

    print(args)
    print(args.output_path)

    stopping_condition = False
    threshold = float(args.threshold)
    if args.threshold_type == "top_entity_by_mention":
        assert args.mention_threshold is not None
        mention_threshold = float(args.mention_threshold)
    else:
        mention_threshold = threshold
    if args.interactive:
        while not stopping_condition:

            logger.info("interactive mode")

            # Interactive
            text = input("insert text: ")

            # Prepare data
            samples = [{"id": "-1", "text": text}]
            dataloader = _process_biencoder_dataloader(
                samples, biencoder.tokenizer, biencoder_params, logger,
            )

            # Run inference
            nns, dists, pred_mention_bounds, mention_scores, cand_scores = _run_biencoder(
                args, biencoder, dataloader, candidate_encoding, samples=samples,
                num_cand_mentions=args.num_cand_mentions, num_cand_entities=args.num_cand_entities,
                device="cpu" if biencoder_params["no_cuda"] else "cuda",
                threshold=mention_threshold, indexer=indexer,
            )

            action = "c"
            while action == "c":
                all_entity_preds = get_predictions(
                    args, dataloader, biencoder_params,
                    samples, nns, dists, mention_scores, cand_scores,
                    pred_mention_bounds, id2title, threshold=threshold,
                    mention_threshold=mention_threshold,
                )[0]

                pred_triples = all_entity_preds[0]['pred_triples']
                _print_colorful_text(all_entity_preds[0]['tokens'], tokenizer, pred_triples)
                _print_colorful_prediction(all_entity_preds, pred_triples, id2text, id2kb)
                action = input("Next question [n] / change threshold [c]: ")
                while action != "n" and action != "c":
                    action = input("Next question [n] / change threshold [c]: ")
                if action == "c":
                    print("Current threshold {}".format(threshold))
                    while True:
                        try:
                            threshold = float(input("New threshold (increase for less cands, decrease for more cands): "))
                            break
                        except:
                            print("Error expected float, got {}".format(threshold))
    
    else:
        samples = None

        samples, num_unk = _get_test_samples(
            args.test_mentions, args.test_entities, logger,
        )
        logger.info("Preparing data for biencoder....")
        dataloader = _process_biencoder_dataloader(
            samples, biencoder.tokenizer, biencoder_params, logger,
        )
        logger.info("Finished preparing data for biencoder")

        stopping_condition = True

        # prepare the data for biencoder
        # run biencoder if predictions not saved
        if not os.path.exists(os.path.join(args.save_preds_dir, 'biencoder_mention_bounds.npy')):

            # run biencoder
            logger.info("Running biencoder...")

            start_time = time.time()
            nns, dists, pred_mention_bounds, mention_scores, cand_scores = _run_biencoder(
                args, biencoder, dataloader, candidate_encoding, samples=samples,
                num_cand_mentions=args.num_cand_mentions, num_cand_entities=args.num_cand_entities,
                device="cpu" if biencoder_params["no_cuda"] else "cuda",
                threshold=mention_threshold, indexer=indexer,
            )
            end_time = time.time()
            logger.info("Finished running biencoder")

            runtime = end_time - start_time
            
            _save_biencoder_outs(
                args.save_preds_dir, nns, dists, pred_mention_bounds, cand_scores, mention_scores, runtime,
            )
        else:
            nns, dists, pred_mention_bounds, cand_scores, mention_scores, runtime = _load_biencoder_outs(args.save_preds_dir)

        assert len(samples) == len(nns) == len(dists) == len(pred_mention_bounds) == len(cand_scores) == len(mention_scores)

        (
            all_entity_preds, num_correct_weak, num_correct_strong, num_predicted, num_gold,
            num_correct_weak_from_input_window, num_correct_strong_from_input_window, num_gold_from_input_window,
        ) = get_predictions(
            args, dataloader, biencoder_params,
            samples, nns, dists, mention_scores, cand_scores,
            pred_mention_bounds, id2title, threshold=threshold,
            mention_threshold=mention_threshold,
        )
        
        print()
        if num_gold > 0:
            print("WEAK MATCHING")
            display_metrics(num_correct_weak, num_predicted, num_gold)
            print("Just entities within input window...")
            display_metrics(num_correct_weak_from_input_window, num_predicted, num_gold_from_input_window)
            print("*--------*")
            print("STRONG MATCHING")
            display_metrics(num_correct_strong, num_predicted, num_gold)
            print("Just entities within input window...")
            display_metrics(num_correct_strong_from_input_window, num_predicted, num_gold_from_input_window)
            print("*--------*")
            print("biencoder runtime = {}".format(runtime))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--debug_biencoder", "-db", action="store_true", default=False, help="Debug biencoder"
    )
    # evaluation mode
    parser.add_argument(
        "--get_predictions", "-p", action="store_true", default=False, help="Getting predictions mode. Does not filter at crossencoder step."
    )
    
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="Interactive mode."
    )

    # test_data
    parser.add_argument(
        "--test_mentions", dest="test_mentions", type=str, help="Test Dataset."
    )
    parser.add_argument(
        "--test_entities", dest="test_entities", type=str, help="Test Entities."
    )

    parser.add_argument(
        "--save_preds_dir", type=str, help="Directory to save model predictions to."
    )
    parser.add_argument(
        "--mention_threshold", type=str, default=None,
        dest="mention_threshold",
        help="Used if threshold type is `top_entity_by_mention`. "
        "Threshold for mention score, for which mentions will be pruned if they fall under that threshold. "
        "Set to '-inf' to get all mentions."
    )
    parser.add_argument(
        "--threshold", type=str, default="-4.5",
        dest="threshold",
        help="Threshold for final joint score, for which examples will be pruned if they fall under that threshold. "
        "Set to '-inf' to get all entities."
    )
    parser.add_argument(
        "--num_cand_mentions", type=int, default=50, help="Number of mention candidates to consider per example (at most)"
    )
    parser.add_argument(
        "--num_cand_entities", type=int, default=10, help="Number of entity candidates to consider per mention (at most)"
    )
    parser.add_argument(
        "--threshold_type", type=str, default=None,
        choices=["joint", "top_entity_by_mention"],
        help="How to threshold the final candidates. "
        "`top_entity_by_mention`: get top candidate (with entity score) for each predicted mention bound. "
        "`joint`: by thresholding joint score."
    )


    # biencoder
    parser.add_argument(
        "--biencoder_model",
        dest="biencoder_model",
        type=str,
        # default="models/biencoder_wiki_large.bin",
        default="models/biencoder_wiki_large.bin",
        help="Path to the biencoder model.",
    )
    parser.add_argument(
        "--biencoder_config",
        dest="biencoder_config",
        type=str,
        # default="models/biencoder_wiki_large.json",
        default="models/biencoder_wiki_large.json",
        help="Path to the biencoder configuration.",
    )
    parser.add_argument(
        "--entity_catalogue",
        dest="entity_catalogue",
        type=str,
        # default="models/tac_entity.jsonl",  # TAC-KBP
        default="models/entity.jsonl",  # ALL WIKIPEDIA!
        help="Path to the entity catalogue.",
    )
    parser.add_argument(
        "--entity_token_ids",
        dest="entity_token_ids",
        type=str,
        default="models/entity_token_ids_128.t7",  # ALL WIKIPEDIA!
        help="Path to the tokenized entity titles + descriptions.",
    )
    parser.add_argument(
        "--entity_encoding",
        dest="entity_encoding",
        type=str,
        # default="models/tac_candidate_encode_large.t7",  # TAC-KBP
        default="models/all_entities_large.t7",  # ALL WIKIPEDIA!
        help="Path to the entity catalogue.",
    )
    parser.add_argument(
        "--faiss_index", type=str, default="hnsw", choices=["hnsw", "flat", "ivfflat", "none"], help="whether to use faiss index",
    )
    parser.add_argument(
        "--index_path", type=str, default="models/faiss_hnsw_index.pkl", help="path to load indexer",
    )

    parser.add_argument(
        "--eval_batch_size",
        dest="eval_batch_size",
        type=int,
        default=8,
        help="Crossencoder's batch size for evaluation",
    )
    parser.add_argument(
        "--max_context_length",
        dest="max_context_length",
        type=int,
        help="Maximum length of context. (Don't set to inherit from training config)",
    )

    # output folder
    parser.add_argument(
        "--output_path",
        dest="output_path",
        type=str,
        default="output",
        help="Path to the output.",
    )

    parser.add_argument(
        "--use_cuda", dest="use_cuda", action="store_true", default=False, help="run on gpu"
    )

    args = parser.parse_args()

    logger = utils.get_logger(args.output_path)
    logger.setLevel(10)

    models = load_models(args, logger)
    run(args, logger, *models)
