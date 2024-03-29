# -*- coding: utf-8 -*-

import torch
import json
from torch.utils.data import DataLoader, Dataset
import os
import numpy as np
import faiss
from preprocess import normalize_string
from utils import sample_range_excluding,OrderedSet
import random


# Entity ExtractorSet (EE)

class ExtractorSet(Dataset):
    def __init__(self, entities):
        self.entities = entities

    def __len__(self):
        return len(self.entities)

    def __getitem__(self, index):
        entity = self.entities[index]
        entity_token_ids = torch.tensor(entity['text_ids']).long()
        entity_masks = torch.tensor(entity['text_masks']).long()
        return entity_token_ids, entity_masks


# For embedding all the mentions during inference
class MentionSet(Dataset):
    def __init__(self, mentions, max_len, tokenizer,
                 add_topic=True, use_title=False
                 ):
        self.mentions = mentions
        self.max_len = max_len
        self.tokenizer = tokenizer
        self.add_topic = add_topic
        self.use_title = use_title
        # [2] is token id of '[unused1]' for bert tokenizer
        self.TT = [2]

    def __len__(self):
        return len(self.mentions)

    def __getitem__(self, index):
        mention = self.mentions[index]
        if self.add_topic:
            title = mention['title'] if self.use_title else mention['topic']
            title_ids = self.TT + title
        else:
            title_ids = []
        # CLS + mention ids + TT + title ids
        mention_title_ids = mention['text']+title_ids
        mention_ids = (mention_title_ids + [self.tokenizer.pad_token_id] * (
                self.max_len - len(mention_title_ids)))[:self.max_len]
        mention_masks = ([1] * len(mention_title_ids) + [0] * (
                self.max_len - len(mention_title_ids)))[:self.max_len]
        mention_token_ids = torch.tensor(mention_ids).long()
        mention_masks = torch.tensor(mention_masks).long()
        return mention_token_ids, mention_masks


def get_labels(samples, all_entity_map):
    # get labels for samples
    labels = []
    for sample in samples:
        entities = sample['entities']
        label_list = [all_entity_map[normalize_string(e)] for
                      e in
                      entities if e in all_entity_map]
        labels.append(label_list)
    labels = np.array(labels)
    return labels


def get_group_indices(samples):
    # get list of group indices for passages come from the same document
    doc_ids = np.unique([s['doc_id'] for s in samples])
    group_indices = {k: [] for k in doc_ids}
    for i, s in enumerate(samples):
        doc_id = s['doc_id']
        group_indices[doc_id].append(i)
    return list(group_indices.values())


def get_entity_map(entities):
    #  get all entity map: map from entity title to index
    entity_map = {}
    for i, e in enumerate(entities):
        entity_map[e['title']] = i
    assert len(entity_map) == len(entities)
    return entity_map


class RetrievalSet(Dataset):
    def __init__(self, mentions, entities, labels, max_len,
                 tokenizer, candidates,
                 num_cands, rands_ratio, type_loss,
                 add_topic=True, use_title=False):
        self.mentions = mentions
        self.candidates = candidates
        self.max_len = max_len
        self.tokenizer = tokenizer
        self.labels = labels
        self.num_cands = num_cands
        self.rands_ratio = rands_ratio
        self.all_entity_token_ids = np.array([e['text_ids'] for e in entities])
        self.all_entity_masks = np.array([e['text_masks'] for e in entities])
        self.entities = entities
        self.type_loss = type_loss
        self.add_topic = add_topic
        self.use_title = use_title
        # '[unused1]' for bert tokenizer
        self.TT = [2]

    def __len__(self):
        return len(self.mentions)


    def __getitem__(self, index):
        """
        :param index: The index of mention
        :return: mention_token_ids,mention_masks,entity_token_ids,entity_masks : 1 X L
                entity_hard_token_ids, entity_hard_masks: k X L  (k<=10)
        """
        # process mention
        mention = self.mentions[index]
        if self.add_topic:
            title = mention['title'] if self.use_title else mention['topic']
            title_ids = self.TT + title
        else:
            title_ids = []
        # CLS + mention ids + TT + title ids
        mention_title_ids = mention['text'] + title_ids
        mention_ids = mention_title_ids + [self.tokenizer.pad_token_id] * (
                self.max_len - len(mention_title_ids))
        mention_masks = [1] * len(mention_title_ids) + [0] * (
                self.max_len - len(mention_title_ids))
        mention_token_ids = torch.tensor(mention_ids[:self.max_len]).long()
        mention_masks = torch.tensor(mention_masks[:self.max_len]).long()
        # process entity
        cand_ids = []
        labels = self.labels[index]
        # dummy labels if there is no label entity for the given passage
        if len(labels) == 0:
            labels = [-1]
        else:
            labels = list(set(labels))
        cand_ids += labels
        num_pos = len(labels)
        # assert num_pos >= 0
        num_neg = self.num_cands - num_pos
        assert num_neg >= 0
        num_rands = int(self.rands_ratio * num_neg)
        num_hards = num_neg - num_rands
        # non-hard and non-label for random negatives
        rand_cands = sample_range_excluding(len(self.entities), num_rands,
                                            set(labels).union(set(
                                                self.candidates[index])))
        cand_ids += rand_cands
        # process hard negatives
        if self.candidates is not None:
            # hard negatives
            hard_negs = random.sample(list(set(self.candidates[index]) - set(
                labels)), num_hards)
            cand_ids += hard_negs
        passage_labels = torch.tensor([1] * num_pos + [0] * num_neg).long()
        candidate_token_ids = self.all_entity_token_ids[cand_ids].tolist()
        candidate_masks = self.all_entity_masks[cand_ids].tolist()
        assert passage_labels.size(0) == self.num_cands
        candidate_token_ids = torch.tensor(candidate_token_ids).long()
        assert candidate_token_ids.size(0) == self.num_cands
        candidate_masks = torch.tensor(candidate_masks).long()
        return mention_token_ids, mention_masks, candidate_token_ids, \
               candidate_masks, passage_labels


def extractor_dataloader(data_dir, kb_dir):
    """

    :param data_dir
    :return: mentions, entities,doc
    """
    print('begin loading data')

    def load_mentions(part):
        with open(os.path.join(data_dir, 'tokenized_aida_%s.json' % part)) as f:
            mentions = json.load(f)
        return mentions

    samples_train = load_mentions('train')
    samples_val = load_mentions('val')
    samples_test = load_mentions('test')

    def load_entities():
        entities = []
        with open(os.path.join(kb_dir, 'entities_kilt.json')) as f:
            for line in f:
                entities.append(json.loads(line))

        return entities

    entities = load_entities()

    return samples_train, samples_val, samples_test, entities


def get_embeddings(loader, model, is_mention, device):
    model.eval()
    embeddings = []
    with torch.no_grad():
        for i, batch in enumerate(loader):
            batch = tuple(t.to(device) for t in batch)
            input_ids, input_masks = batch
            k1, k2 = ('mention_token_ids', 'mention_masks') if is_mention else \
                ('entity_token_ids', 'entity_masks')
            kwargs = {k1: input_ids, k2: input_masks}
            j = 0 if is_mention else 2
            embed = model(**kwargs)[j].detach()
            embeddings.append(embed.cpu().numpy())
    embeddings = np.concatenate(embeddings, axis=0)
    model.train()
    return embeddings


def get_hard_negative(mention_embeddings, all_entity_embeds, k,
                      max_num_postives,
                      use_gpu_index=False):
    index = faiss.IndexFlatIP(all_entity_embeds.shape[1])
    if use_gpu_index:
        index = faiss.index_cpu_to_all_gpus(index)
    index.add(all_entity_embeds)
    scores, hard_indices = index.search(mention_embeddings,
                                        k + max_num_postives)
    del mention_embeddings
    del index
    return hard_indices, scores


def make_single_loader(data_set, bsz, shuffle):
    loader = DataLoader(data_set, bsz, shuffle=shuffle)
    return loader


def get_loader_from_candidates(samples, entities, labels, max_len,
                               tokenizer, candidates,
                               num_cands, rands_ratio, type_loss,
                               add_topic, use_title, shuffle, bsz
                               ):
    data_set = RetrievalSet(samples, entities, labels,
                            max_len, tokenizer, candidates,
                            num_cands, rands_ratio, type_loss, add_topic,
                            use_title)
    loader = make_single_loader(data_set, bsz, shuffle)
    return loader


def extractor_getloaders(samples_train, samples_val, samples_test, entities, max_len,
                tokenizer, mention_bsz, entity_bsz, add_topic,
                use_title):
    #  get all mention and entity dataloaders
    train_mention_set = MentionSet(samples_train, max_len, tokenizer,
                                   add_topic, use_title)
    val_mention_set = MentionSet(samples_val, max_len, tokenizer, add_topic,
                                 use_title)
    test_mention_set = MentionSet(samples_test, max_len, tokenizer, add_topic,
                                  use_title)
    entity_set = ExtractorSet(entities)
    entity_loader = make_single_loader(entity_set, entity_bsz, False)
    train_men_loader = make_single_loader(train_mention_set, mention_bsz,
                                          False)
    val_men_loader = make_single_loader(val_mention_set, mention_bsz, False)
    test_men_loader = make_single_loader(test_mention_set, mention_bsz, False)

    return train_men_loader, val_men_loader, test_men_loader, entity_loader


def save_candidates(mentions, candidates, entity_map, labels, out_dir, part):
    # save results for reader training
    assert len(mentions) == len(candidates)
    labels = labels.tolist()
    out_path = os.path.join(out_dir, '%s.json' % part)
    entity_titles = np.array(list(entity_map.keys()))
    fout = open(out_path, 'w')
    for i in range(len(mentions)):
        mention = mentions[i]
        m_candidates = candidates[i].tolist()
        m_spans = [[s[0], s[1] - 1] for s in mention['spans']]
        assert len(mention['entities']) == len(mention['spans'])
        ent_span_dict = {k: [] for k in mention['entities']}
        for j, l in enumerate(mention['entities']):
            ent_span_dict[l].append(m_spans[j])
        if part == 'train':
            positives = [c for c in m_candidates if c in labels[i]]
            negatives = [c for c in m_candidates if c not in labels[i]]
            pos_titles = entity_titles[positives].tolist()
            pos_spans = [ent_span_dict[p] for p in pos_titles]
            gold_ids = list(set(labels[i]))
            gold_titles = entity_titles[gold_ids].tolist()
            gold_spans = [ent_span_dict[g] for g in gold_titles]
            neg_spans = [[[0, 0]]] * len(negatives)
            item = {'doc_id': mention['doc_id'],
                    'mention_idx': i,
                    'mention_ids': mention['text'],
                    'positives': positives,
                    'negatives': negatives,
                    'labels': mention['entities'],
                    'label_spans': m_spans,
                    'gold_ids': gold_ids,
                    'gold_spans': gold_spans,
                    'pos_spans': pos_spans,
                    'neg_spans': neg_spans,
                    'offset': mention['offset'],
                    'title': mention['title'],
                    'topic': mention['topic'],
                    'passage_labels': [1] * len(positives) + [0] * len(
                        negatives)
                    }
        else:
            candidate_titles = entity_titles[m_candidates]
            candidate_spans = [ent_span_dict[s] if s in ent_span_dict else
                               [[0, 0]] for s in candidate_titles]
            passage_labels = [1 if c in mention['entities'] else 0 for c in
                              candidate_titles]
            item = {'doc_id': mention['doc_id'],
                    'mention_idx': i,
                    'candidates': m_candidates,
                    'title': mention['title'],
                    'topic': mention['topic'],
                    'mention_ids': mention['text'],
                    'labels': mention['entities'],
                    'label_spans': m_spans,
                    'label_ids': labels[i],
                    'offset': mention['offset'],
                    'candidate_spans': candidate_spans,
                    'passage_labels': passage_labels
                    }
        fout.write('%s\n' % json.dumps(item))
    fout.close()


# Mention Matcher (MM)

class MatcherData(Dataset):
    # get the input data item for the reader model
    def __init__(self,
                 tokenizer,
                 samples,
                 entities,
                 max_len,
                 max_num_candidates,
                 is_training,
                 add_topic=False,
                 use_title=False):
        self.tokenizer = tokenizer
        self.is_training = is_training
        self.samples = samples
        self.entities = entities
        self.all_entity_token_ids = np.array([e['text_ids'] for e in entities])
        self.all_entity_masks = np.array([e['text_masks'] for e in entities])
        self.max_len = max_len
        self.max_num_candidates = max_num_candidates
        self.add_topic = add_topic
        self.use_title = use_title
        self.TT = [2]

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        sample = self.samples[index]
        title = None
        if self.add_topic:
            title = sample['title'] if self.use_title else sample['topic']
        mention_ids = sample['mention_ids']
        passage_labels = sample['passage_labels'][:self.max_num_candidates]
        if self.add_topic:
            title_ids = self.TT + title
        else:
            title_ids = []
        if self.is_training:
            positives = sample['positives']
            pos_spans = sample['pos_spans']
            assert len(positives) == len(pos_spans)
            # ensure always have positive labels for training
            if len(positives) == 0:
                positives = sample['gold_ids']
                pos_spans = sample['gold_spans']
                passage_labels = ([1] * len(positives) + passage_labels)[
                                 :self.max_num_candidates]
            negatives = list(np.random.permutation(sample['negatives']))
            candidates = (positives + negatives)[:self.max_num_candidates]
            spans = (pos_spans + sample['neg_spans'])[
                    :self.max_num_candidates]
        else:
            candidates = sample['candidates'][:self.max_num_candidates]
            spans = sample['candidate_spans'][:self.max_num_candidates]
        candidates_ids = self.all_entity_token_ids[candidates]
        candidates_masks = self.all_entity_masks[candidates]

        encoded_pairs = torch.zeros((self.max_num_candidates,
                                     self.max_len)).long()
        type_marks = torch.zeros((self.max_num_candidates, self.max_len)).long()
        attention_masks = torch.zeros((self.max_num_candidates,
                                       self.max_len)).long()
        answer_masks = torch.zeros((self.max_num_candidates,
                                    self.max_len)).long()
        passage_labels = torch.tensor(passage_labels).long()
        if self.is_training:
            start_labels = torch.zeros((self.max_num_candidates,
                                        self.max_len)).long()
            end_labels = torch.zeros((self.max_num_candidates,
                                      self.max_len)).long()
        for i, candidate_ids in enumerate(candidates_ids):
            if self.is_training:
                _spans = np.array(spans[i])
                start_labels[i, _spans[:, 0]] = 1
                end_labels[i, _spans[:, 1]] = 1
            candidate_ids = candidate_ids.tolist()
            candidate_masks = candidates_masks[i].tolist()
            # CLS mention ids TT title ids SEP candidate ids SEP
            input_ids = mention_ids[:-1] + title_ids + [
                self.tokenizer.sep_token_id] + candidate_ids[1:]
            input_ids = (input_ids + [self.tokenizer.pad_token_id] * (
                    self.max_len - len(input_ids)))[:self.max_len]
            attention_mask = [1] * (len(mention_ids + title_ids)) + \
                             candidate_masks[1:]
            attention_mask = (attention_mask + [0] * (self.max_len - len(
                attention_mask)))[:self.max_len]
            token_type_ids = [0] * len(mention_ids + title_ids) + \
                             candidate_masks[1:]
            token_type_ids = (token_type_ids + [0] * (self.max_len - len(
                token_type_ids)))[:self.max_len]
            encoded_pairs[i] = torch.tensor(input_ids)
            attention_masks[i] = torch.tensor(attention_mask)
            type_marks[i] = torch.tensor(token_type_ids)
            answer_masks[i, :len(mention_ids)] = 1
        if self.is_training:
            return encoded_pairs, attention_masks, type_marks, answer_masks, \
                   passage_labels, start_labels, end_labels
        else:
            return encoded_pairs, attention_masks, type_marks, answer_masks, \
                   passage_labels


def matcher_dataloader(data_dir, kb_dir):
    def read_data(part):
        name = '%s.json' % part
        items = []
        with open(os.path.join(data_dir, name)) as f:
            for line in f:
                item = json.loads(line)
                items.append(item)
        return items

    samples_train = read_data('train')
    samples_dev = read_data('val')
    samples_test = read_data('test')

    def load_entities():
        entities = []
        with open(os.path.join(kb_dir, 'entities_kilt.json')) as f:
            for line in f:
                entities.append(json.loads(line))

        return entities

    entities = load_entities()

    return samples_train, samples_dev, samples_test, entities

# get document level gold results
def get_golds(samples_train, samples_dev, samples_test):
    def get_passage_gold(samples):
        p_golds = []
        for sample in samples:
            assert len(sample['labels']) == len(sample['label_spans'])
            # start,end,entity
            g = [span + [entity] for span, entity in zip(sample['label_spans'],
                                                         sample['labels'])]
            p_golds.append(g)
        return p_golds

    p_golds_train = get_passage_gold(samples_train)
    p_golds_val = get_passage_gold(samples_dev)
    p_golds_test = get_passage_gold(samples_test)
    golds_train_doc = get_results_doc(p_golds_train, samples_train)
    golds_val_doc = get_results_doc(p_golds_val, samples_dev)
    golds_test_doc = get_results_doc(p_golds_test, samples_test)
    return golds_train_doc, golds_val_doc, golds_test_doc, p_golds_train, \
           p_golds_val, p_golds_test


def matcher_getloaders(tokenizer, data, max_len,
                max_num_candidates,
                max_num_candidates_val,
                train_bsz, val_bsz,
                add_topic, use_title):
    samples_train, samples_dev, samples_test, entities = data
    train_set = MatcherData(tokenizer, samples_train, entities, max_len,
                           max_num_candidates, True,
                           add_topic, use_title)
    dev_set = MatcherData(tokenizer, samples_dev, entities, max_len,
                         max_num_candidates_val, False, add_topic,
                         use_title)
    test_set =MatcherData(tokenizer, samples_test, entities, max_len,
                          max_num_candidates_val, False, add_topic,
                          use_title)
    loader_train = make_single_loader(train_set, train_bsz, True)
    loader_dev = make_single_loader(dev_set, val_bsz, False)
    loader_test = make_single_loader(test_set, val_bsz, False)
    return loader_train, loader_dev, loader_test


def get_results_doc(passage_results, samples):
    # get document level results from passage-level results
    assert len(passage_results) == len(samples)
    results = []
    # p: start, end, entity_name
    for p, sample in zip(passage_results, samples):
        offset = sample['offset']
        if len(p) == 0:
            continue
        for r in p:
            result = (sample['doc_id'], r[0] + offset, r[1] + offset, r[2])
            results.append(result)
    # result: doc_id, start_doc,end_doc,entity_name
    results = list(OrderedSet(results))
    return results


# save passage level results
def save_results(predicts, p_golds, samples, results_dir, part):
    assert len(predicts) == len(p_golds)
    assert len(samples) == len(predicts)
    save_path = os.path.join(results_dir, 'reader_%s_results.json' % part)
    results = []
    for p_gold, predict, sample in zip(p_golds, predicts, samples):
        result = {}
        result['doc_id'] = sample['doc_id']
        result['text'] = sample['mention_ids']
        result['predicts'] = predict
        result['golds'] = p_gold
        results.append(result)
    with open(save_path, 'w') as f:
        for r in results:
            f.write('%s\n' % json.dumps(r))

