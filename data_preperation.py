import torch
from torch.utils.data import Dataset
import numpy as np
from nltk import word_tokenize
from nltk.tokenize import ToktokTokenizer
import math
import random
import os
from collections import Counter, deque
import torch.multiprocessing as multiprocessing
from torch.multiprocessing import Process
from torch.multiprocessing import Queue
from threading import Thread
from queue import Empty
from datetime import datetime
from functools import partial

USE_CUDA = torch.cuda.is_available()
gpus = [0]
torch.cuda.set_device(0)
FloatTensor = torch.cuda.FloatTensor if USE_CUDA else torch.FloatTensor
LongTensor = torch.cuda.LongTensor if USE_CUDA else torch.LongTensor


class SingleFileDataset(Dataset):
    """Dataset to prepare a single file of Skip-Gram entries"""

    def __init__(self, path, word_vocab, char_to_index, min_count, window,
                 unigram_table, num_total_words, archive, neg_samples=5):
        self.min_count = min_count
        self.path = path
        self.w_vocab = word_vocab
        self.c_to_index = char_to_index
        self.unk = char_to_index['UNK']
        self.total_w = num_total_words
        self.window = window
        self.table = unigram_table
        self.neg_samples = neg_samples
        self.id = os.path.basename(path).split('.')[0]
        self.archive = archive
        tmp = []
        for i, (word, target) in enumerate(self.prepare_file(self.path)):
            tmp.append((word, target))
        self.archive[self.id] = tmp

    def __getitem__(self, idx):
        word, target = self.archive[self.id][idx]
        negs = self.get_negatives(word)
        negs = [self.prepare_tensor(neg) for neg in negs]
        example = [self.prepare_tensor(word), self.prepare_tensor(target)]
        example += negs
        return example

    def __len__(self):
        print('Length: {}'.format(len(self.archive[self.id])))
        return len(self.archive[self.id])

    def prepare_tensor(self, word):
        return prepare_tensor(word, self.c_to_index)

    def get_negatives(self, word):
        to_return = []
        while len(to_return) < self.neg_samples:
            neg = random.choice(self.table)
            if neg == word.lower():
                continue
            to_return.append(neg)
        return to_return

    def prepare_file(self, filepath):
        with open(filepath, 'r', encoding='utf8') as f:
            for line in f:
                for word, target in self.prepare_line(line):
                    yield word, target

    def prepare_line(self, line):
        words = word_tokenize(line)
        max_j = len(words)
        for i, word in enumerate(words):
            if word.lower() not in self.w_vocab:
                continue
            if self.w_vocab[word.lower()] < self.min_count:
                continue
            frequency = self.w_vocab[word.lower()] / self.total_w
            number = 1 - math.sqrt(10e-5/frequency)
            if random.uniform(0, 1) <= number:
                continue
            for j in range(i - self.window, i + self.window):
                if (i == j) or (j < 0) or (j >= max_j):
                    continue
                target = words[j]
                if target.lower() not in self.w_vocab:
                    continue
                yield word, target


class DataLoaderMultiFiles(object):
    """DataLoader to iterator over a set of DataSet"""

    def __init__(self, dataset, batch_s):
        self.dataset = dataset
        self.batch_size = batch_s
        self.index_queue = deque(torch.randperm(len(self.dataset)).tolist())
        self.batch_queue = Queue(maxsize=10)

    def __iter__(self):
        print('here')
        args = (self.batch_queue, self.index_queue, self.dataset,
                self.batch_size)
        self.batch_process = Process(target=fill_batch, args=args)
        self.batch_process.daemon = True
        self.batch_process.start()
        return self

    def done_files(self):
        # return sum([e.is_alive() for e in self.buffr_processes])
        return not self.batch_process.is_alive()

    def __next__(self):
        print('get batch')
        print('batch_queue: {}'.format(self.batch_queue.qsize()))
        timeout = 1 if self.done_files() == 0 else 600
        try:
            batch = self.batch_queue.get(timeout=timeout)
        except Empty:
            print('empty')
            self.kill()
            raise StopIteration
        print('got batch')
        tmp = LongTensor(batch)
        print('computing')
        return tmp

    def kill(self):
        print('Killing processes')
        self.batch_process.terminate()

    def __del__(self):
        self.kill()


def fill_batch(batch_queue, index_queue, dataset, batch_size):
    batch = []
    counter = 0
    print('filling batch')
    while len(index_queue) > 0:
        index = index_queue.pop()
        example = dataset[index]
        batch.extend(example)
        counter += 1
        print('here')
        if counter == batch_size:
            print('at batcvh size')
            counter = 0
            batch_queue.put(pad_sequences(batch))


def pad_sequences(sequences):
    max_len = 0
    for sequence in sequences:
        if sequence.shape[0] > max_len:
            max_len = sequence.shape[0]
    padded_sequences = np.zeros([len(sequences), max_len])
    for i, sequence in enumerate(sequences):
        padded_sequences[i, :sequence.shape[0]] = sequence
    return padded_sequences


def prepare_tensor(word, char_to_index):
    unk = char_to_index['UNK']
    indices = [char_to_index['{']]
    for char in word:
        indices.append(char_to_index.get(char, unk))
    indices.append(char_to_index['}'])
    return np.array(indices)


def build_vocab_multi(directory_path, min_count, num_workers):
    func = partial(build_vocabs, min_count=min_count)
    p = multiprocessing.Pool(num_workers)
    word_counter = Counter()
    char_counter = Counter()
    char_counter.update(['{', '}'])
    counter = 0
    for x, y in p.imap(func, directory_path):
        counter += 1
        print(counter)
        word_counter += x
        char_counter += y
    return word_counter, char_counter


def build_vocabs(filepath, min_count):
    """Build the word and char counter vocabularies"""
    toktok = ToktokTokenizer()
    word_vocab = Counter()
    char_vocab = Counter()
    with open(filepath, 'r', encoding='utf8') as f:
        try:
            line = f.read()
            if 'numbers_' in filepath:
                tmp = toktok.tokenize(line.lower())
                for i in range(min_count):
                    word_vocab.update(tmp)
            else:
                word_vocab.update(word_tokenize(line.lower()))
            char_vocab.update(line)
        except Exception as error:
            print('Error with file: {}'.format(filepath))
            print(error)
    return word_vocab, char_vocab


def build_utilities(word_vocab, char_vocab, vocab_size, min_cnt):
    mst_commn = [e[0] for e in char_vocab.most_common(vocab_size-4)]
    char_list = ['PAD', '{', '}'] + mst_commn + ['UNK']
    char_to_index = {e: n for n, e in enumerate(char_list)}
    index_to_char = {n: e for n, e in enumerate(char_list)}

    word_vocab = {w: n for w, n in word_vocab.items()
                  if (n >= min_cnt) & (len(w) <= 30)}
    num_total_words = sum([num for word, num in word_vocab.items()])
    unigram_table = []
    Z = 0.001
    for word in word_vocab:
        tmp = word_vocab[word]/num_total_words
        unigram_table.extend([word] * int(((tmp)**0.75)/Z))

    return dict(char_to_index=char_to_index, index_to_char=index_to_char,
                unigram_table=unigram_table, num_total_words=num_total_words,
                word_vocab=word_vocab)


def build_dataset(paths, num_workers, word_vocab, char_to_index, min_count,
                  window, unigram_table, num_total_words, neg_samples,
                  archive):
    func = partial(SingleFileDataset, word_vocab=word_vocab, window=window,
                   char_to_index=char_to_index, min_count=min_count,
                   unigram_table=unigram_table, neg_samples=neg_samples,
                   num_total_words=num_total_words, archive=archive)
    p = multiprocessing.Pool(4)
    datasets = []
    for x in p.imap(func, paths):
        print('here')
        datasets.append(x)
    return torch.utils.data.ConcatDataset(datasets)
