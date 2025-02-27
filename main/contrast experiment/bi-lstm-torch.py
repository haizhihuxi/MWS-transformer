import os
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
import pandas as pd
from haversine import haversine, Unit
from sklearn.metrics import mean_squared_error
import matplotlib.pyplot as plt
from tqdm import tqdm

reset = True

# Load the provided data files
test_data_path = '../data/test_filter.pkl'
train_data_path = '../data/train_erp_dtw.pkl'
valid_data_path = '../data/valid_filter.pkl'

test_data = pd.read_pickle(test_data_path)
train_data = pd.read_pickle(train_data_path)
valid_data = pd.read_pickle(valid_data_path)


# Helper function to create sequences
def create_sequences(data, seq_length=18, y_length=24):
    xs, ys = [], []
    for i in range(len(data) - seq_length - y_length):
        x = data[i:i + seq_length]
        y = data[i + seq_length:i + seq_length + y_length]
        xs.append(x)
        ys.append(y)
    xs = np.array([np.array(xi) for xi in xs])
    ys = np.array([np.array(yi) for yi in ys])
    return xs, ys


# Prepare training data
train_data = [x for x in train_data if not np.isnan(x["traj"]).any() and len(x["traj"]) > 42]
test_data = [x for x in test_data if not np.isnan(x["traj"]).any() and len(x["traj"]) > 42]
train_trajectories = [traj['traj'][:, 0:2] for traj in train_data]

# Validate dimensions before creating sequences
# for i, traj in enumerate(train_trajectories):
#     print(f"轨迹 {i} 的形状: {traj.shape}")

# Create training sequences
train_sequences = [create_sequences(traj, 18) for traj in train_trajectories]
X_train = np.concatenate([seq[0] for seq in train_sequences if len(seq[0]) > 0])
y_train = np.concatenate([seq[1] for seq in train_sequences if len(seq[1]) > 0])

# Prepare validation data
valid_trajectories = [traj['traj'][:, 0:2] for traj in test_data]

# Validate dimensions before creating sequences
# for i, traj in enumerate(valid_trajectories):
#     print(f"轨迹 {i} 的形状: {traj.shape}")

# Create validation sequences
valid_sequences = [create_sequences(traj, 18) for traj in valid_trajectories]
X_valid = np.concatenate([seq[0] for seq in valid_sequences if len(seq[0]) > 0])
y_valid = np.concatenate([seq[1] for seq in valid_sequences if len(seq[1]) > 0])

# Ensure all sequences have the same number of features
assert X_train.shape[2] == X_valid.shape[2], "训练数据和验证数据的特征数不匹配"


# Define the BiLSTM model in PyTorch
class BiLSTMModel(nn.Module):
    def __init__(self, input_size):
        super(BiLSTMModel, self).__init__()
        self.bilstm = nn.LSTM(input_size, 64, batch_first=True, bidirectional=True)
        self.lstm = nn.LSTM(128, 64, batch_first=True)
        self.fc = nn.Linear(64, 24 * input_size)

    def forward(self, x):
        x, _ = self.bilstm(x)
        x, _ = self.lstm(x)
        x = self.fc(x[:, -1, :])
        return x


# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device = torch.device("cpu")
print(f"Using device: {device}")

# Prepare data loaders
train_dataset = TensorDataset(torch.tensor(X_train, dtype=torch.float32),
                              torch.tensor(y_train.reshape(y_train.shape[0], -1), dtype=torch.float32))
valid_dataset = TensorDataset(torch.tensor(X_valid, dtype=torch.float32),
                              torch.tensor(y_valid.reshape(y_valid.shape[0], -1), dtype=torch.float32))
train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
valid_loader = DataLoader(valid_dataset, batch_size=32, shuffle=False)

# Initialize model, loss function, and optimizer
model = BiLSTMModel(X_train.shape[2]).to(device)
criterion = nn.MSELoss()
optimizer = optim.Adam(model.parameters(), lr=0.001)

if reset:
    # Training loop
    model.train()
    best_val_loss = float('inf')
    for epoch in range(100):
        running_loss = 0.0
        with tqdm(total=len(train_loader), desc=f"Epoch {epoch + 1}/{1}", unit="batch") as pbar:
            for inputs, targets in train_loader:
                inputs, targets = inputs.to(device), targets.to(device)
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                optimizer.step()
                running_loss += loss.item()
                pbar.set_postfix(loss=running_loss / (pbar.n + 1))
                pbar.update(1)

        # Validation step
        model.eval()
        val_loss = 0.0
        with tqdm(total=len(valid_loader), desc="Validating", unit="batch") as pbar:
            with torch.no_grad():
                for inputs, targets in valid_loader:
                    inputs, targets = inputs.to(device), targets.to(device)
                    outputs = model(inputs)
                    loss = criterion(outputs, targets)
                    val_loss += loss.item()
                    pbar.set_postfix(val_loss=val_loss / (pbar.n + 1))
                    pbar.update(1)

        val_loss /= len(valid_loader)
        print(f"Epoch {epoch + 1}, Training Loss: {running_loss / len(train_loader)}, Validation Loss: {val_loss}")

        # Save the best model
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save(model.state_dict(), 'best_ship_trajectory_bilstm_model.pth')
        model.train()

# Load the best model
model.load_state_dict(torch.load('best_ship_trajectory_bilstm_model.pth'))
model.eval()

# Use the best model for prediction and error calculation
X_valid_tensor = torch.tensor(X_valid, dtype=torch.float32).to(device)
with torch.no_grad():
    y_pred = model(X_valid_tensor).cpu().numpy()

# Reshape predictions and true values back to original shape
y_pred = y_pred.reshape((y_pred.shape[0], 24, -1))
y_valid_coords = y_valid.reshape((y_valid.shape[0], 24, -1))


# Calculate haversine distance error for each prediction point
def calculate_haversine_distance(y_true, y_pred):
    distances = []
    for i in range(y_true.shape[0]):
        traj_true = y_true[i]
        traj_pred = y_pred[i]
        traj_distances = []
        for j in range(traj_true.shape[0]):
            coord_true = (traj_true[j, 0], traj_true[j, 1])
            coord_pred = (traj_pred[j, 0], traj_pred[j, 1])
            distance = haversine(coord_true, coord_pred, unit=Unit.METERS)
            traj_distances.append(distance)
        distances.append(traj_distances)
    return np.array(distances)


errors = calculate_haversine_distance(y_valid_coords, y_pred)


# Calculate RMSE for each sample
def calculate_rmse(y_true, y_pred):
    rmses = []
    for i in range(y_true.shape[0]):
        rmse = mean_squared_error(y_true[i], y_pred[i], squared=False)
        rmses.append(rmse)
    return np.array(rmses)


rmse_errors = calculate_rmse(y_valid_coords, y_pred)
overall_avg_rmse = np.mean(rmse_errors)

# Print overall average RMSE error
print(f"整体平均RMSE误差: {overall_avg_rmse:.4f} meters")

def denormalize_coords(coords, min_val, max_val):
    return coords * (max_val - min_val) + min_val

# 定义经度和纬度的最小值和最大值
lat_min, lat_max = 10.3, 13.0
lon_min, lon_max = 55.5, 58.0

results_folder = 'lstm-results/ori-lstm/result_picture'
os.makedirs(results_folder, exist_ok=True)

for i in range(y_valid_coords.shape[0]):
    print(f'绘制第{i}条轨迹，一共{y_valid_coords.shape[0]}条')

    # 反归一化处理
    inputs_lat = denormalize_coords(X_valid[i, :, 0], lat_min, lat_max)
    inputs_lon = denormalize_coords(X_valid[i, :, 1], lon_min, lon_max)
    true_lat = denormalize_coords(y_valid_coords[i, :, 0], lat_min, lat_max)
    true_lon = denormalize_coords(y_valid_coords[i, :, 1], lon_min, lon_max)
    pred_lat = denormalize_coords(y_pred[i, :, 0], lat_min, lat_max)
    pred_lon = denormalize_coords(y_pred[i, :, 1], lon_min, lon_max)

    plt.figure()
    plt.plot(inputs_lon, inputs_lat, 'g-', label='Input')  # Plot input values
    plt.plot(true_lon, true_lat, 'b-', label='True')
    plt.plot(pred_lon, pred_lat, 'r--', label='Predicted')
    plt.title(f'Sample {i + 1}, Avg Error: {np.mean(errors[i]):.2f} meters')
    plt.xlabel('Longitude')
    plt.ylabel('Latitude')
    plt.legend()
    plt.tight_layout()
    sample_plot_path = os.path.join(results_folder, f'sample_{i + 1}.png')
    plt.savefig(sample_plot_path)
    plt.close()
# Convert errors from meters to kilometers
errors_km = errors / 1000

# Calculate average error for each point and overall average error
pointwise_avg_errors_km = np.mean(errors_km, axis=0)
overall_avg_error_km = np.mean(errors_km)

# Print average error for each point
print("每个点的平均误差（千米）：")
for i, error in enumerate(pointwise_avg_errors_km):
    print(f"Point {i + 1}: {error:.2f} kilometers")

print(f"整体平均哈弗塞恩距离误差: {overall_avg_error_km:.4f} kilometers")

# Create results folder
results_folder = 'lstm-results/bilstm'
os.makedirs(results_folder, exist_ok=True)

# Save RMSE and haversine distance errors to file
rmse_file_path = os.path.join(results_folder, 'average_rmse_error.txt')
with open(rmse_file_path, 'w') as file:
    file.write(f"整体平均RMSE误差: {overall_avg_rmse:.4f} meters\n")
    file.write(f"整体平均哈弗塞恩距离误差: {overall_avg_error_km:.4f} kilometers\n")

# Save errors to CSV file
errors_df = pd.DataFrame({
    'Point Index': range(1, len(pointwise_avg_errors_km) + 1),
    'Average Haversine Distance Error (kilometers)': pointwise_avg_errors_km
})
csv_path = os.path.join(results_folder, 'average_haversine_distance_errors.csv')
errors_df.to_csv(csv_path, index=False)

# Plot average error for each point and save as PNG file
plt.figure(figsize=(12, 6))
plt.plot(range(1, len(pointwise_avg_errors_km) + 1), pointwise_avg_errors_km, 'bo-')
for i in range(0, len(pointwise_avg_errors_km), 6):
    plt.annotate(f'{pointwise_avg_errors_km[i]:.2f}',
                 (i + 1, pointwise_avg_errors_km[i]),
                 textcoords="offset points",
                 xytext=(0, 10),
                 ha='center',
                 fontsize=8)
    plt.plot(i + 1, pointwise_avg_errors_km[i], 'ro')  # Mark points
plt.xlabel('Point Index')
plt.ylabel('Average Haversine Distance Error (kilometers)')
plt.title('Average Haversine Distance Error for Each Point')
plt.grid(True)
png_path = os.path.join(results_folder, 'average_haversine_distance_errors.png')
plt.savefig(png_path)
plt.show()
