echo "Creating cluster..."
kind create cluster --name slack-2022-02 --config kind-config.yaml

echo "Labeling nodes..."
kubectl label namespace default istio-injection=enabled
kubectl label nodes slack-2022-02-worker5 role=app
kubectl label nodes slack-2022-02-worker4 role=cache
kubectl label nodes slack-2022-02-worker3 role=cache
kubectl label nodes slack-2022-02-worker2 role=cache
kubectl label nodes slack-2022-02-worker role=database

echo "Installing istio..."
istioctl install --set profile=demo -y

echo "Applying configurations..."
kubectl apply -f configs.yaml
kubectl apply -f infrastructure.yaml
kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.29/samples/addons/prometheus.yaml

echo "Waiting for pods to initialize..."
until kubectl wait --for=condition=Ready pod --all -n default --timeout=10s >/dev/null 2>&1; do
  echo -n "."
  sleep 2
done

echo "Initializing (hopefully) database and cache"
kubectl exec deploy/slack-clone-app -c app -- python app.py
