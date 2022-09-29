from abc import ABC, abstractmethod

class GraphSampler(ABC):
    """
    Samples a graph based on some properties/models and returns a sampled graph.
    """
    def __init__(self):
        pass

    """
    Samples the original graph and returns the sampled graph.

    Args:
        original_graph (dgl.Graph): The graph to be sampled

    Returns:
        sampled_graph (dgl.Graph): The graph left after sampling unnecessary nodes.
    """
    def sample_graph(self, original_graph):
        pass