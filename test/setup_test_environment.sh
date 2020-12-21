# Copyright (c) 2020 SIGHUP s.r.l All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

# Build & push Docker image
cd ..
docker build . -t mariouhrik/external-dns-ui:latest
docker push mariouhrik/external-dns-ui:latest
cd -

# Deploy a Minikube test environment
if minikube status | grep -ri running; then minikube status; else echo "Please start minikube via minikube start" && exit 1; fi
if minikube addons list | grep ingress | grep enabled; then echo "Minikube ingress addon is enabled"; else echo "Please enable Ingress addon for minikube via minikube addons enable ingress" && exit 2; fi
kubectl -n external-dns delete deployment external-dns-ui external-dns
kubectl -n external-dns delete pod -l app=external-dns-ui
kustomize build | kubectl apply -f -
kubectl apply -f test_ingresses.yaml -n testns
echo "Waiting for the external-dns-ui pod to get Ready..."
sleep 8
kubectl -n external-dns wait --for=condition=Ready pod -l app=external-dns-ui
EXTERNAL_DNS_POD_NAME=$(kubectl -n external-dns get pod -l app=external-dns-ui -o custom-columns=:metadata.name --no-headers=true)
kubectl -n external-dns exec -it $EXTERNAL_DNS_POD_NAME -- sh -c "echo \"10.0.2.15     testdomain.com\" >> /etc/hosts"
kubectl -n external-dns exec -it $EXTERNAL_DNS_POD_NAME -- sh -c "echo \"10.0.2.15     gatekeeper2.testdomain.com\" >> /etc/hosts"

# Setup port-forward, so that you can view the web app at localhost:8000
echo "-----------------------TEST ENVIRONMENT SETUP COMPLETED SUCCESSFULLY----------------------------"
echo "Please, use the following command:"
echo ""
echo "              kubectl -n external-dns port-forward svc/external-dns-ui 8000:80"
echo ""
echo "Afterwards, you can use your web browser and visit the web app at localhost:8000"
