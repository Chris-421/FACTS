import pandas as pd
import numpy as np

rng = np.random.default_rng(seed=42)

factory_df = pd.read_excel("Padmanabhan et al.xlsx")

print(factory_df)

products = factory_df["Products"].unique()
operations = factory_df["Operations"].unique()
machines = factory_df["Machines"].unique()

op_df = factory_df.pivot(index="Operations", columns="Products", values="Processing_time")

m_df = factory_df.groupby("Machines").agg(
    Operations=("Operations", lambda x: x.dropna().unique().tolist())
)
m_df["Number"] = rng.integers(1, 5, size=len(m_df))

p_df = (
    factory_df
    .sort_values(["Products", "Sequence"])
    .groupby("Products")
    .agg(Operations=("Operations", list))
)

print(op_df)
print(m_df)
print(p_df)

n_orders = 100

today = pd.Timestamp.today()
start_date = today.replace(day=1).normalize()
end_date = start_date + pd.offsets.MonthBegin(1)

orders = []

for i in range(1, n_orders + 1):
    # Random number of distinct products per order
    n_products = rng.integers(1, len(products) + 1)

    # Random selection of unique products
    selected_products = rng.choice(
        products, size=n_products, replace=False
    )

    # Random quantities per product
    quantities = rng.integers(1, 11, size=n_products)

    # Random request date
    request_date = start_date + pd.Timedelta(
        days=int(rng.integers(0, (end_date - start_date).days))
    )

    orders.append({
        "Order_ID": i,
        "Products": selected_products.tolist(),
        "Quantities": quantities.tolist(),
        "Request_Date": request_date
    })

orders_df = pd.DataFrame(orders)

print(orders_df)

with pd.ExcelWriter("Factory_Model.xlsx", engine="openpyxl") as writer:
    factory_df.to_excel(writer, sheet_name="Raw_Data", index=False)
    m_df.to_excel(writer, sheet_name="Machines")
    op_df.to_excel(writer, sheet_name="Operations")
    p_df.to_excel(writer, sheet_name="Products")
    orders_df.to_excel(writer, sheet_name="Orders", index=False)