import ollama

# 配置远程地址
REMOTE_HOST = "http://192.168.20.22:11434" 
MODEL_NAME = "qwen3.5:27b"
client = ollama.Client(host=REMOTE_HOST)

def encode_image(image_path: str) -> str:
    """将图片编码为 base64"""
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')
    
def chat_with_remote(prompt):
    try:
        # 关键：通过 client 参数指定远程 host
        response = client.chat(
            model=MODEL_NAME,  # 注意是 qwen2.5 不是 qwen3.5
            messages=[
                {'role': 'system', 'content': '# nanobot 🐈You are nanobot, a helpful AI assistant.'}, 
                {'role': 'user', 'content': prompt},
#                 {'role': 'assistant', 'content': '', 'images':['/Users/peterluck3/Documents/Myshit/codes/python/AMP/docs/AMP平台介绍截图.jpg']}
                {'role': 'tool', 'tool_call_id': 'LrqkCapul', 'name': 'read_image', 'content':'我已帮用户读取了图片的base64数据，请根据上下文对这张图片的base64数据进行分析。', 'images': ['/Users/peterluck3/Downloads/test.jpg']}
            ],
        )
#         return response['message']['content']
        return response
    
    except Exception as e:
        return f"连接失败: {e}"

# 测试调用
if __name__ == "__main__":
    print(f"正在连接远程模型 {MODEL_NAME}...")
    result = chat_with_remote("给我分析一下待会儿得到的图片。")
    print("回复:", result)