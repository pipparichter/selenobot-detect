'''A binary classification head and associated utilities, designed for handling embedding objects.'''

from tqdm import tqdm
from typing import Optional, NoReturn, Tuple, List
from selenobot.datasets import get_dataloader
import sys
import torch
import os
import numpy as np
import pandas as pd
import torch.nn.functional
import sklearn
import json
from selenobot.utils import NumpyEncoder
import warnings
import copy

warnings.simplefilter('ignore')
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'


class WeightedBCELoss(torch.nn.Module):
    '''Defining a class for easily working with weighted Binary Cross Entropy loss.'''
    def __init__(self, weight=1):

        super(WeightedBCELoss, self).__init__()
        self.w = weight

    def forward(self, outputs, targets):
        '''Update the internal states keeping track of the loss.'''
        # Make sure the outputs and targets have the same shape.
        outputs = outputs.reshape(targets.shape)
        # reduction specifies the reduction to apply to the output. If 'none', no reduction will be applied, if 'mean,' the weighted mean of the output is taken.
        ce = torch.nn.functional.binary_cross_entropy(outputs, targets, reduction='none')
        # Seems to be generating a weight vector, so that the weight is applied to indices marked with a 1. This
        # should have the effect of increasing the cost of a false negative.
        w = torch.where(targets == 1, self.w, 1).to(DEVICE)

        return (ce * w).mean()


class Classifier(torch.nn.Module):
    '''Class defining the binary classification head.'''

    attrs = ['epochs', 'batch_size', 'lr', 'val_losses', 'train_losses', 'best_epoch']

    def __init__(self, 
        hidden_dim:int=512,
        input_dim:int=1024,
        bce_loss_weight:float=1,
        random_seed:int=42):
        '''
        Initializes a two-layer linear classification head. 

        :param bce_loss_weight: The weight applied to false negatives in the BCE loss function. 
        :param hidden_dim: The number of nodes in the second linear layer of the two-layer classifier.
        :param input_dim: The dimensionality of the input embedding. 
        '''
        # Initialize the torch Module
        super().__init__()
        torch.manual_seed(random_seed)

        self.classifier = torch.nn.Sequential(
            torch.nn.Linear(input_dim, hidden_dim),
            torch.nn.ReLU(),
            torch.nn.Linear(hidden_dim, 1),
            torch.nn.Sigmoid())

        # Initialize model weights according to which activation is used.'''
        torch.nn.init.kaiming_normal_(self.classifier[0].weight)
        torch.nn.init.xavier_normal_(self.classifier[2].weight)

        self.to(DEVICE)
        self.loss_func = WeightedBCELoss(weight=bce_loss_weight)

        # Parameters to be populated when the model has been fitted. 
        self.best_epoch = None
        self.epochs = None
        self.lr = None 
        self.batch_size = None
        self.train_losses, self.val_losses = None, None
        
    # TODO: Do I still need the batch size parameter here?
    def forward(self, inputs:torch.FloatTensor, low_memory:bool=True):
        '''A forward pass of the Classifier.'''
        assert inputs.dtype == torch.float32, f'classifiers.Classifier.forward: Expected input embedding of type torch.float32, not {inputs.dtype}.'

        # if low_memory:
        #     outputs = []
        #     chunk_size = 2
        #     n_chunks = len(inputs) // chunk_size + 1
        #     for i in tqdm(range(n_chunks)):
        #         chunk = inputs[i:(i + 1) * chunk_size]
        #         outputs.append(self.classifier(chunk))
        #     return torch.concat(outputs)
        
        # else:
        return self.classifier(inputs) 

    def predict(self, dataset, threshold:float=None) -> torch.Tensor:
        '''Evaluate the Classifier on the data in the input Dataset.'''   
        self.eval() # Put the model in evaluation mode. 
        outputs = self(dataset.embeddings) # Run a forward pass of the model. Batch to limit memory usage. 
        self.train() # Put the model back in train mode.

        if threshold is not None: # Apply the threshold to the output values. 
            outputs = np.ones(outputs.shape) # Initialize an array of ones. 
            outputs[np.where(outputs < threshold)] = 0

        return outputs

    def _loss(self, dataset):
        outputs, targets = self(dataset.embeddings), dataset.labels 
        return self.loss_func(outputs, targets)

    def fit(self, train_dataset, val_dataset, epochs:int=10, lr:float=0.01, batch_size:int=16):
        '''Train Classifier model on the data in the DataLoader.

        :param dataloader: The DataLoader object containing the training data. 
        :param val_dataset: The Dataset object containing the validation data. Expected to be a Dataset as defined in datasets.py.
        :param epochs: The number of epochs to train for. 
        :param lr: The learning rate. 
        :param bce_loss_weight: The weight to be passed into the WeightedBCELoss constructor.
        :param batch_size: The size of the batches to use for model training.
        '''

        self.train() # Put the model in train mode.

        optimizer = torch.optim.Adam(self.parameters(), lr=lr)

        best_epoch, best_model_weights = 0, None

        # Want to log the initial training and validation metrics.
        val_losses, train_losses = [np.inf], [np.inf]

        dataloader = get_dataloader(train_dataset, batch_size=batch_size)
        for epoch in tqdm(range(epochs), desc='Classifier.fit'):
            train_loss = []
            for batch in dataloader:
                # Evaluate the model on the batch in the training dataloader. 
                outputs, targets = self(batch['embedding']), batch['label']
                loss = self.loss_func(outputs, targets)
                loss.backward()
                train_loss.append(loss.item()) # Store the batch loss to compute training loss across the epoch. 
                optimizer.step()
                optimizer.zero_grad()
            
            train_losses.append(np.mean(train_loss))
            val_losses.append(self._loss(val_dataset).item())

            if val_losses[-1] < min(val_losses[:-1]):
                best_epoch = epoch
                best_model_weights = copy.deepcopy(self.state_dict())

        print(f'Classifier.fit: Best model weights encountered at epoch {epoch}.')
        self.load_state_dict(best_model_weights) # Load the best model weights. 

        # Save training values in the model. 
        self.best_epoch = best_epoch
        self.val_losses = val_losses
        self.train_losses = train_losses
        self.epochs = epochs
        self.batch_size = batch_size

    def save(self, path:str):
        info = dict()
        for attr in Classifier.attrs:
            info[attr] = getattr(self, attr)
        info['state_dict'] = self.state_dict() #.numpy()
        print(info)

        with open(path, 'w') as f:
            json.dump(info, f, cls=NumpyEncoder)

    def load(self, path:str):
        pass
        


class SimpleClassifier(Classifier):
    '''Class defining a simplified version of the binary classification head.'''

    def __init__(self, 
        bce_loss_weight:float=1.0,
        input_dim:int=1,
        random_seed:int=42):
        '''Initializes a single-layer linear classification head.'''

        # Initialize the torch Module. The classifier attribute will be overridden by the rest of this init function. 
        super().__init__()
        torch.manual_seed(random_seed)
        
        self.classifier = torch.nn.Sequential(
            torch.nn.Linear(input_dim, 1),
            torch.nn.Sigmoid())

        # Initialize model weights according to which activation is used.
        torch.nn.init.kaiming_normal_(self.classifier[0].weight)
        self.to(DEVICE)

        self.loss_func = WeightedBCELoss(weight=bce_loss_weight)



# def optimize(dataloader, val_dataset:Dataset, n_calls:int=50): 

#     # Probably keep the weights as integers, at least for now. 
#     search_space = [skopt.space.Real(1, 10, name='bce_loss_weight')]

#     lr = 0.001
#     epochs = 10

#     # Create the objective function. Decorator allows named arguments to be inferred from search space. 
#     @skopt.utils.use_named_args(dimensions=search_space)
#     def objective(bce_loss_weight=None):
#         model = Classifier(hidden_dim=512, input_dim=1024, bce_loss_weight=bce_loss_weight) # Reset the model. 
#         model.fit(dataloader, val_dataset, epochs=epochs, lr=lr, bce_loss_weight=bce_loss_weight)
        
#         # Evaluate the performance of the fitted model on the data.
#         info = model.predict(val_dataset)
#         return -info.get_balanced_accuracy() # Return negative so as not to minimize accuracy.
    
#     result = skopt.gp_minimize(objective, search_space)
#     return result.x





 





