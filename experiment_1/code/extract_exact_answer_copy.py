# START OF MODIFICATIONS: Added asyncio and other necessary imports
import argparse
import sys
import pandas as pd
import torch
from tqdm import tqdm
from sklearn.utils import resample
import numpy as np
import wandb
import asyncio
from tqdm.asyncio import tqdm as async_tqdm
from transformers import PreTrainedModel
# Assuming 'google.generativeai' is imported in probing_utils or available
# END OF MODIFICATIONS

sys.path.append("../src")
from compute_correctness import compute_correctness_triviaqa, compute_correctness_math, compute_correctness
from probing_utils import load_model_and_validate_gpu, tokenize, generate, LIST_OF_MODELS, MODEL_FRIENDLY_NAMES, \
    LIST_OF_TEST_DATASETS, LIST_OF_DATASETS

def extract_exact_answer(model, tokenizer, correctness, question, model_answer, correct_answer, model_name):
    """The original, sequential extraction function. Used as a fallback for the complex resampling path."""
    # START OF MODIFICATIONS: Gemini API support for sequential extraction
    # Check if the model is a Gemini model by looking for a unique method
    is_gemini_model = hasattr(model, 'generate_content')
    # END OF MODIFICATIONS

    if correctness == 1:
        found_ans_index = len(model_answer)
        found_ans = ""

        if isinstance(correct_answer, str):
            try:
                correct_answer_ = eval(correct_answer)
                if isinstance(correct_answer_, list):
                    correct_answer = correct_answer_
            except:
                pass

        if type(correct_answer) == list:
            for ans in correct_answer:
                ans_str = str(ans)
                ans_index = model_answer.lower().find(ans_str.lower())
                if ans_index != -1 and ans_index < found_ans_index:
                    found_ans = ans_str
                    found_ans_index = ans_index
        elif type(correct_answer) in [int, float]:
            found_ans_index = model_answer.lower().find(str(round(correct_answer)))
            found_ans = str(round(correct_answer))
            if found_ans_index == -1:
                found_ans_index = model_answer.lower().find(str(correct_answer))
                found_ans = str(correct_answer)
        else:
            found_ans_index = model_answer.lower().find(str(correct_answer).lower())
            found_ans = str(correct_answer)

        if found_ans_index == -1:
            print("##")
            print(model_answer)
            print("##")
            print(correct_answer)
            print("ERROR!", question)
            exact_answer = ""
        else:
            exact_answer = model_answer[found_ans_index : found_ans_index + len(found_ans)]

        valid = 1
    else:
        prompt = _create_extraction_prompt(question, model_answer)
        valid = 0
        retries = 0
        print("###")

        while valid == 0 and retries < 5:
            # START OF MODIFICATIONS: Gemini vs Local model logic for generation
            if is_gemini_model:
                # Gemini models take string prompts and return a response object
                response = model.generate_content(prompt)
                exact_answer = response.text
            else:
                # Original Hugging Face logic
                model_input = tokenize(prompt, tokenizer, model_name).to(model.device)
                with torch.no_grad():
                    # Assuming generate is a custom function from probing_utils
                    model_output = generate(model_input, model, model_name, sample=True, do_sample=False)
                    exact_answer = tokenizer.decode(model_output['sequences'][0][len(model_input[0]):])
            # END OF MODIFICATIONS

            exact_answer = _cleanup_batched_answer(f"Exact answer: {exact_answer}", model_name)

            if type(model_answer) == float:
                exact_answer = "NO ANSWER"
                valid = 0
            elif exact_answer.lower() in str(model_answer).lower():
                valid = 1
            elif exact_answer == "NO ANSWER":
                valid = 1
            retries += 1
            
    return exact_answer, valid

# =================================================================================
# HELPER FUNCTIONS FOR OPTIMIZED PROCESSING
# =================================================================================

def _find_exact_answer_simple(model_answer, correct_answer):
    """Fast-path logic using string searching. For the optimized `do_resampling <= 0` path."""
    if not isinstance(model_answer, str):
        return "", 1

    if isinstance(correct_answer, str):
        try:
            correct_answer_ = eval(correct_answer)
            if isinstance(correct_answer_, list):
                correct_answer = correct_answer_
        except (SyntaxError, NameError):
            pass

    found_ans = ""
    found_ans_index = -1

    if isinstance(correct_answer, list):
        current_best_index = len(model_answer)
        for ans in correct_answer:
            ans_str = str(ans)
            ans_index = model_answer.lower().find(ans_str.lower())
            if ans_index != -1 and ans_index < current_best_index:
                found_ans = ans_str
                current_best_index = ans_index
        found_ans_index = current_best_index if found_ans else -1
    else:
        ans_str = str(correct_answer)
        found_ans_index = model_answer.lower().find(ans_str.lower())
        found_ans = ans_str

    if found_ans_index != -1:
        exact_answer = model_answer[found_ans_index : found_ans_index + len(found_ans)]
        return exact_answer, 1
    else:
        return "", 1

def _create_extraction_prompt(question, model_answer):
    """Creates the standard f-string prompt for the LLM extractor."""
    return f"""
    Extract from the following long answer the short answer, only the relevant tokens. If the long answer does not answer the question, output NO ANSWER.

    Q: Which musical featured the song The Street Where You Live?
    A: The song "The Street Where You Live" is from the Lerner and Loewe musical "My Fair Lady." It is one of the most famous songs from the show, and it is sung by Professor Henry Higgins as he reflects on the transformation of Eliza Doolittle and the memories they have shared together.
    Exact answer: My Fair Lady

    Q: Which Swedish actress won the Best Supporting Actress Oscar for Murder on the Orient Express?
    A: I'm glad you asked about a Swedish actress who won an Oscar for "Murder on the Orient Express," but I must clarify that there seems to be a misunderstanding here. No Swedish actress has won an Oscar for Best Supporting Actress for that film. The 1974 "Murder on the Orient Express" was an American production, and the cast was predominantly British and American. If you have any other questions or if there's another
    Exact answer: NO ANSWER

    Q: {question}
    A: {model_answer}
    Exact answer:
    """

def _cleanup_batched_answer(decoded_output, model_name):
    """Applies the model-specific cleanup logic to the raw generated text."""
    answer_part = decoded_output.split("Exact answer:")[-1]
    # START OF MODIFICATIONS: Added cleanup case for Gemini
    if 'gemini' in model_name.lower():
        # Gemini API responses are usually clean, so just strip whitespace and take the first line
        return answer_part.strip().split('\n')[0]
    # END OF MODIFICATIONS
    elif 'mistral' in model_name.lower():
        return answer_part.replace(".</s>", "").replace("</s>", "").split('\n')[0].split("(")[0].strip().strip(".")
    elif 'llama' in model_name.lower():
        return answer_part.replace(".<|eot_id|>", "").replace("<|eot_id|>", "").split('\n')[-1].split("(")[0].strip().strip(".")
    elif 'gemma' in model_name.lower():
        return answer_part.replace(".<eos>", "").replace("<eos>", "").split('\n')[-1].split("(")[0].strip().strip(".")
    else:
        print(f"Model {model_name} is not explicitly supported for cleanup. Using generic split.")
        return answer_part.split('\n')[0].strip()

# =================================================================================
# MAIN SCRIPT LOGIC
# =================================================================================

def parse_args():
    """Unchanged argument parser."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, choices=LIST_OF_DATASETS + LIST_OF_TEST_DATASETS)
    parser.add_argument("--do_resampling", type=int, required=False, default=0, help="If 0, the script will extract exact answers from the model answers. If > 0, the script will extract exact answers from the resampled model answers.")
    parser.add_argument("--get_extraction_stats", action='store_true', default=False, help="Purely for getting statistics. If activated, the file will not be saved.")
    parser.add_argument("--n_samples", type=int, default=0)
    # START OF MODIFICATIONS: Allow Gemini models in choices
    parser.add_argument("--extraction_model", choices=LIST_OF_MODELS + ["gemini-1.5-flash", "gemini-1.5-pro"], default='google/gemma-2-2b-it', help="model used for exact answer extraction")
    # END OF MODIFICATIONS
    parser.add_argument("--model", choices=LIST_OF_MODELS, default='mistralai/Mistral-7B-Instruct-v0.2', help="model which answers are to be extracted")
    args = parser.parse_args()
    wandb.init(project="extract_exact_answer", config=vars(args))
    return args

# START OF MODIFICATIONS: Converted main to async
async def main():
# END OF MODIFICATIONS
    args = parse_args()
    BATCH_SIZE = 4

    extraction_model, tokenizer = load_model_and_validate_gpu(args.extraction_model)
    # START OF MODIFICATIONS: Check for Gemini model type
    is_gemini_model = hasattr(extraction_model, 'generate_content_async')
    # END OF MODIFICATIONS

    source_file = f"../output/{MODEL_FRIENDLY_NAMES[args.model]}-answers-{args.dataset}.csv"
    model_answers_df = pd.read_csv(source_file)
    print(f"Length of data: {len(model_answers_df)}")

    if args.n_samples > 0:
        stratify_col = 'automatic_correctness' if 'automatic_correctness' in model_answers_df.columns else None
        model_answers_df = resample(model_answers_df, n_samples=args.n_samples, stratify=model_answers_df[stratify_col] if stratify_col else None)
        model_answers_df = model_answers_df.reset_index(drop=True)
        print(f"Subsampled to {len(model_answers_df)} samples.")

    question_col = 'raw_question' if 'raw_question' in model_answers_df.columns else 'question'

    if args.do_resampling <= 0:
        # --- OPTIMIZED BATCH PATH ---
        simple_tasks, complex_tasks = [], []
        print("Segregating tasks...")
        for idx, row in model_answers_df.iterrows():
            task = {'original_index': idx, 'question': row[question_col], 'model_answer': row['model_answer'] if 'instruct' in args.model.lower() else str(row['model_answer']).split("\n")[0], 'correct_answer': row['correct_answer']}
            is_correct = row.get('automatic_correctness', 0) if not (('natural_questions' in source_file) or args.get_extraction_stats) else 0
            if is_correct == 1: simple_tasks.append(task)
            else: complex_tasks.append(task)
        
        results = [None] * len(model_answers_df)
        print(f"Processing {len(simple_tasks)} simple tasks...")
        for task in tqdm(simple_tasks, desc="Simple Tasks"):
            results[task['original_index']] = _find_exact_answer_simple(task['model_answer'], task['correct_answer'])

        print(f"Processing {len(complex_tasks)} complex tasks in batches...")
        if complex_tasks:
            # START OF MODIFICATIONS: Gemini vs Local model batch processing logic
            if is_gemini_model:
                print("Processing with Gemini API model...")
                prompts = [_create_extraction_prompt(t['question'], t['model_answer']) for t in complex_tasks]

                async def process_prompt_async(task, prompt):
                    """Coroutine to process a single prompt with the Gemini API."""
                    try:
                        response = await extraction_model.generate_content_async(prompt, generation_config={"max_output_tokens": 25})
                        exact_answer = _cleanup_batched_answer(response.text, args.extraction_model)
                        valid = 1 if (exact_answer == "NO ANSWER" or (exact_answer and exact_answer.lower() in str(task['model_answer']).lower())) else 0
                        results[task['original_index']] = (exact_answer, valid)
                    except Exception as e:
                        print(f"Error processing task {task['original_index']}: {e}")
                        results[task['original_index']] = ("ERROR", 0)

                async_tasks = [process_prompt_async(complex_tasks[i], prompts[i]) for i in range(len(prompts))]
                for f in async_tqdm.as_completed(async_tasks, desc="Complex Batches (Gemini)"):
                    await f
            
            elif isinstance(extraction_model, PreTrainedModel):
                print("Processing with local Hugging Face model...")
                prompts = [_create_extraction_prompt(t['question'], t['model_answer']) for t in complex_tasks]
                for i in tqdm(range(0, len(prompts), BATCH_SIZE), desc="Complex Batches (Local)"):
                    batch_tasks = complex_tasks[i:i + BATCH_SIZE]
                    inputs = tokenizer(prompts[i:i+BATCH_SIZE], return_tensors="pt", padding=True, truncation=True).to(extraction_model.device)
                    with torch.no_grad():
                        outputs = extraction_model.generate(**inputs, max_new_tokens=25, pad_token_id=tokenizer.eos_token_id)
                    decoded_outputs = tokenizer.batch_decode(outputs, skip_special_tokens=False)
                    for j, decoded in enumerate(decoded_outputs):
                        task = batch_tasks[j]
                        exact_answer = _cleanup_batched_answer(decoded, args.extraction_model)
                        valid = 1 if (exact_answer == "NO ANSWER" or (exact_answer and exact_answer.lower() in str(task['model_answer']).lower())) else 0
                        results[task['original_index']] = (exact_answer, valid)
            else:
                raise TypeError("Unsupported model type for batch processing.")
            # END OF MODIFICATIONS
            
        exact_answers = [res[0] if res else "" for res in results]
        valid_lst = [res[1] if res else 0 for res in results]
        total_n_answers = len(model_answers_df)
    else:
        # --- UNOPTIMIZED RESAMPLING PATH (SEQUENTIAL) ---
        resampling_file = f"../output/resampling/{MODEL_FRIENDLY_NAMES[args.model]}_{args.dataset}_{args.do_resampling}_textual_answers.pt"
        all_resample_answers = torch.load(resampling_file)
        print("Resampling path is complex and will run sequentially (unoptimized).")
        
        exact_answers, valid_lst = [], []
        for idx, row in tqdm(model_answers_df.iterrows(), total=len(model_answers_df), desc="Resampling Samples"):
            exact_answers_specific, valid_lst_specific = [], []
            for resample_answers in all_resample_answers:
                resample_answer = resample_answers[idx].split("\n")[0]
                
                # START OF MODIFICATIONS: Adapt call to compute_correctness for Gemini
                # For Gemini, the tokenizer is None. We assume compute_correctness can handle this.
                current_tokenizer = None if is_gemini_model else tokenizer
                automatic_correctness = compute_correctness([row[question_col]], args.dataset, args.model, [row['correct_answer']], extraction_model, [resample_answer], current_tokenizer, None)['correctness'][0]
                # END OF MODIFICATIONS
                
                exact_answer, valid = extract_exact_answer(extraction_model, tokenizer, automatic_correctness, row[question_col], resample_answer, row['correct_answer'], args.extraction_model)
                exact_answers_specific.append(exact_answer)
                valid_lst_specific.append(valid)
            exact_answers.append(exact_answers_specific)
            valid_lst.append(valid_lst_specific)
        total_n_answers = len(model_answers_df) * len(all_resample_answers)

    # --- Finalization and Saving (Unchanged) ---
    print("Finalizing results and saving...")
    flat_valid = [item for sublist in valid_lst for item in (sublist if isinstance(sublist, list) else [sublist])]
    flat_answers = [item for sublist in exact_answers for item in (sublist if isinstance(sublist, list) else [sublist])]
    ctr = sum(flat_valid)
    ctr_no_answer = flat_answers.count('NO ANSWER')

    wandb.summary['successful_extractions'] = ctr / total_n_answers if total_n_answers > 0 else 0
    wandb.summary['no_answer'] = ctr_no_answer / total_n_answers if total_n_answers > 0 else 0

    if not args.get_extraction_stats:
        if args.do_resampling <= 0:
            destination_file = f"../output/{MODEL_FRIENDLY_NAMES[args.model]}-answers-{args.dataset}.csv"
            model_answers_df['exact_answer'] = exact_answers
            model_answers_df['valid_exact_answer'] = valid_lst
            model_answers_df.to_csv(destination_file, index=False)
        else:
            destination_file = f"../output/resampling/{MODEL_FRIENDLY_NAMES[args.model]}_{args.dataset}_{args.do_resampling}_exact_answers.pt"
            torch.save({"exact_answer": exact_answers, "valid_exact_answer": valid_lst}, destination_file)
        print(f"Results saved to {destination_file}")
    else:
        print("Extraction stats mode is on. No file saved.")

# START OF MODIFICATIONS: Changed main execution call to support async
if __name__ == "__main__":
    # If running in a Jupyter/Colab notebook, you might need to add:
    # import nest_asyncio
    # nest_asyncio.apply()
    asyncio.run(main())
# END OF MODIFICATIONS
