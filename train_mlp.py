import argparse
import random
import os
import numpy as np
from tqdm import tqdm
import time
import copy
import os

import torch
from torch import nn
from torch.utils.tensorboard import SummaryWriter
import torch.nn.functional as F
import torch.optim as optim

import dgl
from ogb.linkproppred import Evaluator

# from thop import profile

from datasets import HomogenousNodeClsDataset
from models import GCN, NodePredictorMLP
from evaluators import NodeEvaluator
from graphsampler import FullSampler, UniformEdgeSampler, UniformNodeSampler, DegreeNodeSampler, DegreeEdgeSampler, LabelEdgeSampler, ForestFireSampler, MaskGcnSampler

def set_args_based_on_dataset(args):
    """
    Set the default arguments for some inputs based on the dataset.
    """

    if args.dataset == 'ogbn-products':
        args.batch_size = 10000

def parse_arguments():
    # Training settings
    parser = argparse.ArgumentParser(description='Train GNN')
    parser.add_argument('--device', type = int, default = 0,
                        help = 'which gpu to use if any (0)')
    parser.add_argument('--dataset', type=str, default='ogbn-arxiv',
                        help='dataset name (default: ogbn-arxiv)')
    parser.add_argument('--log_dir', type = str, help = "Log directory to store the tensorboard")
    parser.add_argument('--checkpoint_dir', type = str, help = "Directory to store the model")
    parser.add_argument('--batch_size', type = int, default = 1024)
    parser.add_argument('--neighbours', type = int, default = 10)
    parser.add_argument('--num_workers', type = int, default = 2)
    parser.add_argument('--num_epochs', type = int, default = 100)
    parser.add_argument('--hidden_dim', type = int, default = 256)
    parser.add_argument('--lr', type = float, default = 0.001)
    parser.add_argument('--log_every', type = int, default = 1)
    parser.add_argument('--gnn_layers', type = int, default = 3)
    parser.add_argument('--predictor_layers', type = int, default = 3)
    parser.add_argument('--sampler', type = str, default = 'full', help = "can be full, uniform_edge, uniform_node, degree_node, degree_edge")
    parser.add_argument('--method', type = str, default = 'higher', help = "can be higher or lower")
    parser.add_argument('--prob', type = float, default = 0.5)
    parser.add_argument('--homophilic_prob', type = float, default = 0.5)
    parser.add_argument('--p_f', type = float, default = 0.5)
    parser.add_argument('--dropout', type = float, default = 0.0)
    parser.add_argument('--mask_path', type = str)
    return parser.parse_args()

def set_seeds():
    torch.set_num_threads(1)
    np.random.seed(42)
    torch.manual_seed(42)
    torch.cuda.manual_seed(42)
    random.seed(42)

def sample_graph(dataset, args):
    sampler_type = args.sampler
    if sampler_type == 'full':
        sampler = FullSampler(args.checkpoint_dir)
    elif sampler_type == 'uniform_edge':
        sampler = UniformEdgeSampler(p = args.prob, graph_save_dir = args.checkpoint_dir, directed = dataset.directed)
    elif sampler_type == 'uniform_node':
        sampler = UniformNodeSampler(p = args.prob, graph_save_dir = args.checkpoint_dir)
    elif sampler_type == 'degree_node':
        sampler = DegreeNodeSampler(p = args.prob, graph_save_dir = args.checkpoint_dir, method = args.method)
    elif sampler_type == 'degree_edge':
        sampler = DegreeEdgeSampler(p = args.prob, graph_save_dir = args.checkpoint_dir, method = args.method, directed = dataset.directed)
    elif sampler_type == 'label_edge':
        sampler = LabelEdgeSampler(p = args.prob, homophilic_prob = args.homophilic_prob,
            graph_save_dir = args.checkpoint_dir, directed = dataset.directed)
    elif sampler_type == 'forest_fire':
        sampler = ForestFireSampler(p = args.prob, p_f = args.p_f, graph_save_dir = args.checkpoint_dir)
    elif sampler_type == 'mask_gcn':
        sampler = MaskGcnSampler(mask_path = args.mask_path, p = args.prob, graph_save_dir = args.checkpoint_dir)
    sampled_graph = sampler.sample_graph(dataset.graph)
    dataset.set_sampled_graph(sampled_graph)
    print("Sampled the graph")

def train(model, predictor, dataloader, optimizer, device):
    model.train()
    predictor.train()

    loss_accum = 0
    loss_fn = torch.nn.CrossEntropyLoss()
    macs_sum = 0
    with tqdm(dataloader) as tq:
        for step, (input_nodes, output_nodes, mfgs) in enumerate(tq):
            optimizer.zero_grad()
            
            # TODO(rajabans): Add regularization to the model.
            # import pdb; pdb.set_trace()
            # feat = mfgs[0].srcdata['feat'].to(device)
            feat = mfgs[-1].dstdata['feat'].to(device)
            # macs, _ = profile(model, inputs=(mfgs, feat))
            # macs_sum += macs / 1000000
            # x = model(mfgs, feat)
            x = model(feat)
            output_predictions = predictor(x)
            output_labels = mfgs[-1].dstdata['label']

            loss = loss_fn(output_predictions, output_labels.view(-1))

            loss.backward()

            optimizer.step()

            tq.set_postfix_str(f'loss = {loss.detach().cpu().item()}')
            loss_accum += loss.detach().cpu().item()
    
    # return loss_accum / (step + 1), macs_sum
    return loss_accum / (step + 1)

@torch.no_grad()
def eval(model, predictor, dataset, evaluator, batch_size, neighbours, device, split = 'valid'):
    model.eval()
    predictor.eval()

    feat = dataset.original_graph.ndata['feat'].to(device)
    # Doing the inference over graphs already present in the dataset.
    # infer_embs = model.inference(dataset.original_graph, feat, batch_size, neighbours, device).to(device)
    infer_embs = model(feat)
    
    val_score = evaluator.eval(infer_embs)
    return val_score

def main():
    set_seeds()

    args = parse_arguments()
    set_args_based_on_dataset(args)
    if args.log_dir and not args.checkpoint_dir:
        args.checkpoint_dir = args.log_dir
    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() and args.device >= 0 else torch.device("cpu")
    print(device)
    print(args)

    if args.log_dir != '':
        writer = SummaryWriter(log_dir=args.log_dir)
    else:
        writer = None

    dataset = HomogenousNodeClsDataset(args.dataset)

    # Graph sampling - This is where we do the preprocessing step. Here we sample
    # nodes and edges to reduce the size of the graph.
    sample_graph(dataset, args)

    # Neighbour sampler for sampling the neighbours while constructing the graph
    if args.neighbours == -1:
        neighbour_sampler = dgl.dataloading.MultiLayerFullNeighborSampler(args.gnn_layers)
    else:
        neighbour_sampler = dgl.dataloading.NeighborSampler([args.neighbours for _ in range(args.gnn_layers)])

    train_loader = dgl.dataloading.DataLoader(
        dataset.graph, dataset.train_idx, neighbour_sampler,
        batch_size=args.batch_size, shuffle=True, drop_last=False, num_workers=args.num_workers, device = device)

    model = NodePredictorMLP(dataset.feat_dim, args.hidden_dim, args.hidden_dim, 3).to(device)
    predictor = NodePredictorMLP(args.hidden_dim, args.hidden_dim, dataset.num_classes, 1).to(device)
    evaluator = NodeEvaluator(dataset = dataset, predictor = predictor)
    optimizer = optim.Adam(list(model.parameters()) + list(predictor.parameters()), lr = args.lr)

    best_val_perf = -float('inf')
    result_dict = {
        "args": args.__dict__,
        'epoch_times': [],
        'epoch_times_clock': [],
        # 'macs_sum': [],
    }
    for epoch in range(args.num_epochs):
        start = time.process_time()
        start_clock = time.time()
        print("=====Epoch {}".format(epoch))

        # loss, macs_sum = train(model, predictor, train_loader, optimizer, device)
        loss = train(model, predictor, train_loader, optimizer, device)
        print(f'The loss is {loss}')
        if writer is not None:
            writer.add_scalar('train/loss', loss, epoch)

        if epoch % args.log_every == 0:
            val_perf = eval(model, predictor, dataset, evaluator, args.batch_size, args.neighbours, device)
            print(f'The validation score is {val_perf}')

            if writer is not None:
                writer.add_scalar('valid/score', val_perf, epoch)

            if val_perf > best_val_perf:
                best_val_perf = val_perf
                result_dict['val_perf'] = val_perf
                result_dict['gnn'] = copy.deepcopy(model.state_dict())
                result_dict['epoch'] = epoch

        time_for_epoch = time.process_time() - start
        time_for_epoch_clock = time.time() - start_clock
        result_dict['epoch_times'].append(time_for_epoch)
        result_dict['epoch_times_clock'].append(time_for_epoch_clock)
        # result_dict['macs_sum'].append(macs_sum)
        # TODO(rajabans): Check if this file exists and fail if it does. Give an arg to overwrite if present.
        if args.checkpoint_dir:
            torch.save(result_dict, os.path.join(args.checkpoint_dir, "checkpoint.pt"))
    
    if writer is not None:
        writer.close()

    if args.checkpoint_dir:
        torch.save(result_dict, os.path.join(args.checkpoint_dir, "checkpoint.pt"))

    print(result_dict)

if __name__ == "__main__":
    main()