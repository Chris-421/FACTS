import pandas as pd
import numpy as np
import ast

FILE = "Factory_Model.xlsx"

m_df = pd.read_excel(FILE, sheet_name="Machines", index_col=0)
op_df = pd.read_excel(FILE, sheet_name="Operations", index_col=0)
p_df = pd.read_excel(FILE, sheet_name="Products", index_col=0)
orders_df = pd.read_excel(FILE, sheet_name="Orders")

# Convert Excel-stored lists back into Python lists
def parse_list(value):
    if isinstance(value, (list, np.ndarray)):
        return list(value)
    if isinstance(value, str):
        return ast.literal_eval(value)
    return []

m_df["Operations"] = m_df["Operations"].apply(parse_list)
p_df["Operations_Sequence"] = p_df["Operations_Sequence"].apply(parse_list)
orders_df["Products"] = orders_df["Products"].apply(parse_list)
orders_df["Quantities"] = orders_df["Quantities"].apply(parse_list)

orders_df["Release_Date"] = pd.to_datetime(
    orders_df["Release_Date"]
)

print("Factory model loaded successfully.")

j_df = (
    orders_df[["Order_ID", "Release_Date", "Products", "Quantities"]]
    .explode(["Products", "Quantities"], ignore_index=True)
    .rename(columns={"Products": "Product", "Quantities": "Quantity"})
)

# Give each line a stable ID based on its position in the original order.
line_number = j_df.groupby("Order_ID").cumcount() + 1
j_df["Job_ID"] = j_df["Order_ID"].astype(str) + "-" + line_number.astype(str)

j_df["Quantity"] = pd.to_numeric(j_df["Quantity"], errors="raise")
j_df["Next_Op_Index"] = 0
j_df["Status"] = "waiting"

print(j_df.head)