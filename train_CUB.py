import os
import math
import glob
import json
import random
import argparse
import classifier as classifier2
from utils import *
import torch.nn as nn
import torch.optim as optim
import FrEIA.framework as Ff
import FrEIA.modules as Fm
import torch.nn.functional as F
from time import gmtime, strftime
import torch.backends.cudnn as cudnn
from dataset_GBU import FeatDataLayer, DATA_LOADER
from sklearn.metrics.pairwise import cosine_similarity


parser = argparse.ArgumentParser()
parser.add_argument('--dataset', default='CUB')
parser.add_argument('--dataroot', default='./data', help='path to dataset')
parser.add_argument('--validation', action='store_true', default=False, help='enable cross validation mode')
parser.add_argument('--workers', type=int, help='number of data loading workers', default=4)
parser.add_argument('--image_embedding', default='res101', type=str)
parser.add_argument('--class_embedding', default='att', type=str)

parser.add_argument('--gen_nepoch', type=int, default=600, help='number of epochs to train for')
parser.add_argument('--lr', type=float, default=0.0003, help='learning rate to train generater')
parser.add_argument('--weight_decay', type=float, default=1e-5, help='weight_decay')

parser.add_argument('--classifier_lr', type=float, default=0.01, help='learning rate to train softmax classifier')
parser.add_argument('--batchsize', type=int, default=256, help='input batch size')
parser.add_argument('--nSample', type=int, default=600, help='number features to generate per class')#900
parser.add_argument('--num_coupling_layers', type=int, default=5, help='number of coupling layers')

parser.add_argument('--disp_interval', type=int, default=200)
parser.add_argument('--save_interval', type=int, default=10000)
parser.add_argument('--evl_interval',  type=int, default=500)
parser.add_argument('--manualSeed', type=int, default=7945, help='manual seed')
parser.add_argument('--input_dim',     type=int, default=1024, help='dimension of the global semantic vectors')

parser.add_argument('--prototype',    type=float, default=1, help='weight of the prototype loss')
parser.add_argument('--cprototype',    type=float, default=1, help='weight of the prototype loss')#0.1
parser.add_argument('--cons',    type=float, default=2, help='weight of the prototype loss')
parser.add_argument('--f',    type=float, default=1, help='weight of the prototype loss')
parser.add_argument('--pi', type=float, default=0.11, help='degree of the perturbation')
parser.add_argument('--dropout', type=float, default=0.0, help='probability of dropping a dimension'
                                                               'in the perturbation noise')

parser.add_argument('--zsl', default=False)
parser.add_argument('--clusters', type=int, default=3)

parser.add_argument('--gpu', default="1", help='index of GPU to use')
opt = parser.parse_args()
# os.environ['CUDA_VISIBLE_DEVICES'] = opt.gpu

if opt.manualSeed is None:
    opt.manualSeed = random.randint(1, 10000)
print("Random Seed: ", opt.manualSeed)
np.random.seed(opt.manualSeed)
random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)
torch.cuda.manual_seed_all(opt.manualSeed)

cudnn.benchmark = True
print('Running parameters:')
print(json.dumps(vars(opt), indent=4, separators=(',', ': ')))


def train():
    dataset = DATA_LOADER(opt)
    opt.C_dim = dataset.att_dim
    opt.X_dim = dataset.feature_dim
    opt.y_dim = dataset.ntrain_class
    out_dir = 'out/{}/mask-{}_pi-{}_c-{}_ns-{}_wd-{}_lr-{}_nS-{}_bs-{}_ps-{}'.format(opt.dataset, opt.dropout, opt.pi, opt.prototype,
               opt.nSample, opt.weight_decay, opt.lr, opt.nSample, opt.batchsize, opt.pi)
    os.makedirs(out_dir, exist_ok=True)
    print("The output dictionary is {}".format(out_dir))

    log_dir = out_dir + '/log_{}.txt'.format(opt.dataset)
    with open(log_dir, 'w') as f:
        f.write('Training Start:')
        f.write(strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime()) + '\n')

    dataset.feature_dim = dataset.train_feature.shape[1]
    data_layer = FeatDataLayer(dataset.train_label.numpy(), dataset.train_feature.cpu().numpy(), opt)
    opt.niter = int(dataset.ntrain/opt.batchsize) * opt.gen_nepoch

    result_gzsl_soft = Result()
    sim = cosine_similarity(dataset.train_att, dataset.train_att)
    min_idx = np.argmin(sim.sum(-1))
    min = dataset.train_att[min_idx]
    max_idx = np.argmax(sim.sum(-1))
    max = dataset.train_att[max_idx]
    medi_idx = np.argwhere(sim.sum(-1)==np.sort(sim.sum(-1))[int(sim.shape[0]/2)])
    medi = dataset.train_att[int(medi_idx)]
    vertices = torch.from_numpy(np.stack((min,max,medi))).float().cuda()


    flow = cINN(opt).cuda()

    sm = GSModule(vertices, int(opt.input_dim)).cuda()
    classifier = classifier2.LINEAR_LOGSOFTMAX(opt.input_dim,dataset.ntrain_class + dataset.ntest_class).cuda()
    classifier.apply(classifier2.weights_init)
    cls_criterion = nn.NLLLoss()
    netD = AttDec(opt).cuda()
    netF = Feedback(opt).cuda()
    print(flow)
    netD_optimizer = optim.Adam(netD.parameters(), lr=0.001, betas=(0.5, 0.999))
    cls_optimizer = optim.Adam(classifier.parameters(), lr=0.001, betas=(0.5, 0.999))
    optimizer = optim.Adam(list(flow.trainable_parameters)+list(sm.parameters()), lr=opt.lr, weight_decay=opt.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer=optimizer, gamma=0.8, step_size=15)

    mse = nn.MSELoss()
    with open(log_dir, 'a') as f:
        f.write('\n')
        f.write('GSMFlow Training Start:')
        f.write(strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime()) + '\n')
    start_step = 0
    prototype_loss = 0

    x_mean = torch.from_numpy(dataset.tr_cls_centroid).cuda()
    iters = math.ceil(dataset.ntrain/opt.batchsize)
    for it in range(start_step, opt.niter+1):
        flow.zero_grad()
        sm.zero_grad()
        classifier.zero_grad()
        netD.zero_grad()

        blobs = data_layer.forward()
        feat_data = blobs['data']  # image data
        labels_numpy = blobs['labels'].astype(int)  # class labels
        labels = torch.from_numpy(labels_numpy.astype('int')).cuda()

        C = np.array([dataset.train_att[i, :] for i in labels])
        C = torch.from_numpy(C.astype('float32')).cuda()
        X = torch.from_numpy(feat_data).cuda()

        ############
        all_attr = dataset.attribute.cuda()
        all_label = torch.arange(0,dataset.ntrain_class + dataset.ntest_class,dtype=torch.long).cuda()
        gattr = sm(all_attr)
        cls_out = classifier(gattr)
        cons1 = cls_criterion(cls_out,all_label)

        sr = sm(C)
        z = opt.pi * torch.randn(opt.batchsize, 2048).cuda()
        mask = torch.cuda.FloatTensor(2048).uniform_() > opt.dropout
        z = mask * z
        X = X+z
        z_, log_jac_det = flow(X, sr)

        loss = opt.f*(torch.mean(z_**2) / 2 - torch.mean(log_jac_det) / 2048) + opt.cons*cons1

        loss.backward(retain_graph=True)

        if opt.prototype>0.0:
            with torch.no_grad():
                sr = sm(torch.from_numpy(dataset.train_att).cuda())
            z = torch.zeros(dataset.ntrain_class, 2048).cuda()
            x_ = flow.reverse_sample(z, sr)
            fsr=netD(x_)
            prototype_loss = opt.prototype*mse(x_, x_mean)+opt.cprototype*mse(fsr,sr)
            prototype_loss.backward()
        else:
            prototype_loss=loss


        optimizer.step()
        cls_optimizer.step()
        netD_optimizer.step()
        if it % iters == 0:
            lr_scheduler.step()
        if it % opt.disp_interval == 0 and it:
            log_text = 'Iter-[{}/{}]; loss: {:.3f}; prototype_loss:{:.3f};'.format(it, opt.niter, loss.item(),
                                                                                  prototype_loss.item())
            log_print(log_text, log_dir)

        if it % opt.evl_interval == 0 and it > 2000:
            flow.eval()
            sm.eval()
            netD.eval()
            gen_feat, gen_label = synthesize_feature(flow, sm, dataset, opt)

            train_X = torch.cat((dataset.train_feature, gen_feat), 0)
            train_Y = torch.cat((dataset.train_label, gen_label + dataset.ntrain_class), 0)

            """ GZSL"""

            cls = classifier2.CLASSIFIER(opt, train_X, train_Y, dataset,  dataset.test_seen_feature,  dataset.test_unseen_feature,
                                dataset.ntrain_class + dataset.ntest_class, True, opt.classifier_lr, 0.5, 30, 3000, True)

            result_gzsl_soft.update_gzsl(it, cls.acc_unseen, cls.acc_seen, cls.H)

            log_print("GZSL Softmax:", log_dir)
            log_print("U->T {:.2f}%  S->T {:.2f}%  H {:.2f}%  Best_H [{:.2f}% {:.2f}% {:.2f}% | Iter-{}]".format(
                cls.acc_unseen, cls.acc_seen, cls.H,  result_gzsl_soft.best_acc_U_T, result_gzsl_soft.best_acc_S_T,
                result_gzsl_soft.best_acc, result_gzsl_soft.best_iter), log_dir)

            #ZSL
            cls = classifier2.CLASSIFIER(opt, gen_feat, gen_label, dataset,  dataset.test_seen_feature,  dataset.test_unseen_feature,
                                dataset.ntest_class, True, opt.classifier_lr, 0.5, 30, 1000, False)
            result_gzsl_soft.update(it,cls.acc)
            log_print("GZSL Softmax:", log_dir)
            log_print("U->T {:.2f}%  Best[{:.2f}% | Iter-{}]".format(
                cls.acc,
                result_gzsl_soft.best_acc, result_gzsl_soft.best_iter), log_dir)

            if result_gzsl_soft.save_model:
                files2remove = glob.glob(out_dir + '/Best_model_GZSL_*')
                for _i in files2remove:
                    os.remove(_i)
                np.save(opt.dataset+'.npy', gen_feat.cpu().numpy())
                save_model(it, flow, sm, opt.manualSeed, log_text,
                           out_dir + '/Best_model_GZSL_H_{:.2f}_S_{:.2f}_U_{:.2f}.tar'.format(result_gzsl_soft.best_acc,
                                                                                             result_gzsl_soft.best_acc_S_T,
                                                                                             result_gzsl_soft.best_acc_U_T))

            sm.train()
            flow.train()
            netD.train()
            if it % opt.save_interval == 0 and it:
                save_model(it, flow, sm, opt.manualSeed, log_text,
                           out_dir + '/Iter_{:d}.tar'.format(it))
                print('Save model to ' + out_dir + '/Iter_{:d}.tar'.format(it))

class cINN(nn.Module):
    '''cINN for class-conditional MNISt generation'''
    def __init__(self, opt):
        super().__init__()
        self.opt = opt
        self.cinn = self.build_inn()

        self.trainable_parameters = [p for p in self.cinn.parameters() if p.requires_grad]
        for p in self.trainable_parameters:
            p.data = 0.01 * torch.randn_like(p)

    def build_inn(self):

        def subnet(ch_in, ch_out):
            return nn.Sequential(nn.Linear(ch_in, 2048),
                                 nn.LeakyReLU(0.2),
                                 nn.Linear(2048, ch_out))

        cond = Ff.ConditionNode(1024)
        nodes = [Ff.InputNode(2048)]
        nodes.append(Ff.Node(nodes[-1], Fm.Flatten, {}))

        for k in range(opt.num_coupling_layers):
            # nodes.append(Ff.Node(nodes[-1], Fm.PermuteRandom, {'seed':k}))
            nodes.append(Ff.Node(nodes[-1], Fm.GLOWCouplingBlock,
                                 {'subnet_constructor':subnet, 'clamp':1.0},
                                 conditions=cond))

        return Ff.ReversibleGraphNet(nodes + [cond, Ff.OutputNode(nodes[-1])], verbose=False)

    def forward(self, x, c):
        z = self.cinn(x, c)
        jac = self.cinn.log_jacobian(run_forward=False)
        return z, jac

    def reverse_sample(self, z, c):
        return self.cinn(z, c, rev=True)

class LinearModule(nn.Module):
    def __init__(self, vertice, out_dim):
        super(LinearModule, self).__init__()
        self.register_buffer('vertice', vertice.clone())
        self.fc = nn.Linear(vertice.numel(), out_dim)

    def forward(self, semantic_vec):
        input_offsets = semantic_vec - self.vertice
        response = F.relu(self.fc(input_offsets))
        return response

class GSModule(nn.Module):
    def __init__(self, vertices, out_dim):
        super(GSModule, self).__init__()
        self.individuals = nn.ModuleList()
        assert vertices.dim() == 2, 'invalid shape : {:}'.format(vertices.shape)
        self.out_dim     = out_dim
        self.require_adj = False
        for i in range(vertices.shape[0]):
            layer = LinearModule(vertices[i], out_dim)
            self.individuals.append(layer)

    def forward(self, semantic_vec):
        responses = [indiv(semantic_vec) for indiv in self.individuals]
        global_semantic = sum(responses)
        return global_semantic

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
        m.weight.data.normal_(0.0, 0.02)
        m.bias.data.fill_(0)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)

class AttDec(nn.Module):
    def __init__(self, opt):
        super(AttDec, self).__init__()
        self.embedSz = 0
        self.fc1 = nn.Linear(opt.X_dim, 1024)
        self.fc3 = nn.Linear(1024, opt.input_dim)
        self.lrelu = nn.LeakyReLU(0.2, True)
        self.hidden = None
        self.sigmoid = None
        self.apply(weights_init)

    def forward(self, feat, att=None):
        h = feat
        if self.embedSz > 0:
            assert att is not None, 'Conditional Decoder requires attribute input'
            h = torch.cat((feat,att),1)
        self.hidden = self.lrelu(self.fc1(h))
        h = self.fc3(self.hidden)
        if self.sigmoid is not None: 
            h = self.sigmoid(h)
        else:
            h = h/h.pow(2).sum(1).sqrt().unsqueeze(1).expand(h.size(0),h.size(1))
        self.out = h
        return h

    def getLayersOutDet(self):
        #used at synthesis time and feature transformation
        return self.hidden.detach()

class Feedback(nn.Module):
    def __init__(self,opt):
        super(Feedback, self).__init__()
        self.fc1 = nn.Linear(1024, 1024)
        self.fc2 = nn.Linear(1024, 1024)
        self.lrelu = nn.LeakyReLU(0.2, True)
        self.apply(weights_init)
    def forward(self,x):
        self.x1 = self.lrelu(self.fc1(x))
        h = self.lrelu(self.fc2(self.x1))
        return h


if __name__ == "__main__":
    train()